package com.dissertation.inventoryservice.fault;

import jakarta.annotation.PostConstruct;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Component;

import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

@Component
@RequiredArgsConstructor
@Slf4j
public class F6PodOomKillFault implements Fault{

    private final FaultRegistry registry;

    private final AtomicBoolean active =  new AtomicBoolean(false);

    // Use off-heap buffers to trigger container-level memory limits
    private final List<ByteBuffer> offHeapBuffers = new ArrayList<>();

    private ScheduledExecutorService allocator;

    // Allocation configuration
    private static final int CHUNK_SIZE_MB  = 50;
    private static final int ALLOCATION_INTERVAL_MS = 500;

    @PostConstruct
    private void init(){
        registry.register(this);
    }

    @Override
    public String getId() {
        return "f6";
    }

    @Override
    public String getDescription() {
        return "Rapid off-heap memory spike to trigger Kubernetes OOMKill (pod restart)";
    }

    @Override
    public boolean isActive() {
        return active.get();
    }

    @Override
    public void activate() {
        if(active.compareAndSet(false,true)){
            log.warn("F6: OOMKill fault activated. Allocating off-heap memory.");
            allocator = Executors.newSingleThreadScheduledExecutor(r -> {
                Thread t = new Thread(r, "fault-f6-oom-allocator");
                t.setDaemon(true);
                return t;
            });

            allocator.scheduleAtFixedRate(()->{
                try {
                    int chunkBytes = CHUNK_SIZE_MB * 1024 * 1024;
                    ByteBuffer buffer = ByteBuffer.allocateDirect(chunkBytes);
                    
                    // Force the OS to commit the pages by writing to the buffer
                    for (int i=0; i<chunkBytes; i+=4096){
                        buffer.put(i, (byte) 0xFF);
                    }
                    offHeapBuffers.add(buffer);
                    log.warn("F6: Allocated {} MB off-heap memory. Total Chunks : {} ", chunkBytes, offHeapBuffers.size());
                }
                catch (OutOfMemoryError e){
                    log.error("F6: Native OOM Reached");
                }
            },0, ALLOCATION_INTERVAL_MS, TimeUnit.MILLISECONDS);

        }

    }

    @Override
    public void deactivate() {
        // Pods are usually restarted before deactivation is called in Kubernetes
        if(active.compareAndExchange(true,false)){
            log.info("F6: Deactivating - Releasing buffers");
            if(allocator!=null){
                allocator.shutdownNow();
            }
            offHeapBuffers.clear();
            System.gc(); // Trigger cleanup of direct buffers
            log.info("F6: off-heap buffers released");
        }

    }
}
