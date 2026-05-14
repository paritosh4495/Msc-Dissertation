package com.dissertation.inventoryservice.fault;

import jakarta.annotation.PostConstruct;
import jakarta.annotation.PreDestroy;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;

@Component
@RequiredArgsConstructor
@Slf4j
public class F5SlowMemoryLeakFault implements Fault {

    private final FaultRegistry registry;
    private final AtomicBoolean active = new AtomicBoolean(false);
    private final AtomicBoolean criticallyFull = new AtomicBoolean(false);
    private final AtomicLong leakedBytes = new AtomicLong(0);
    private static final double HEAP_SAFETY_CEILING = 0.90;        // Stop at 90% of -Xmx

    // Thread-safe  collection to ensure objects survive GC cycles
    private  final List<byte[]> leakContainer = Collections.synchronizedList(new ArrayList<>());
    
    private ScheduledExecutorService executor;
    
    // Configuration: leak 2MB every second
    private static final int LEAK_SIZE_BYTES = 2 * 1024 * 1024;
    private static final int LEAK_INTERVAL_SECONDS = 1;

    @PostConstruct
    public void init() {
        registry.register(this);
    }

    @Override
    public String getId() {
        return "f5";
    }

    @Override
    public String getDescription() {
        return "Simulates a slow memory leak causing gradual heap pressure and performance degradation";
    }

    @Override
    public boolean isActive() {
        return active.get();
    }

    @Override
    public synchronized void activate() {
        if (active.compareAndSet(false, true)) {
            criticallyFull.set(false);
            log.warn("F5: Activating slow memory leak. Allocation: {} bytes every {}s", 
                    LEAK_SIZE_BYTES, LEAK_INTERVAL_SECONDS);
            
            executor = Executors.newSingleThreadScheduledExecutor(r -> {
                Thread t = new Thread(r, "fault-f5-leak-generator");
                t.setDaemon(true);
                return t;
            });
            
            executor.scheduleAtFixedRate(this::leakMemory, 0, LEAK_INTERVAL_SECONDS, TimeUnit.SECONDS);
        }
    }

    @Override
    public synchronized void deactivate() {
        if (active.compareAndSet(true, false)) {
            log.info("F5: Deactivating memory leak. Total leaked: {} MB. Clearing container.", 
                    leakedBytes.get() / (1024 * 1024));
            
            if (executor != null) {
                executor.shutdownNow();
                try {
                    executor.awaitTermination(2,TimeUnit.SECONDS);
                }
                catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
                executor = null;
            }
            
            leakContainer.clear();
            leakedBytes.set(0);
            criticallyFull.set(false);
            
            // Suggest a GC to reclaim memory
            System.gc();
            log.info("F5: Container Cleared. CG Hint Issued. Please Allow Stabilisation before the next Trial Begins ---");
        }
    }

    private void leakMemory() {
        if (!active.get() || criticallyFull.get()) return;

        Runtime runtime = Runtime.getRuntime();
        long usedMemory = runtime.totalMemory() - runtime.freeMemory();
        long maxMemory = runtime.maxMemory();
        double heapUsageRatio = (double) usedMemory / maxMemory;

        if (heapUsageRatio >= HEAP_SAFETY_CEILING) {
            if (!criticallyFull.getAndSet(true)) {
                log.warn("F5: Heap ceiling reached ({}% of {}MB max). " +
                                "Halting allocations. Pod protected from OOMKill. " +
                                "Fault active — degraded heap state maintained.",
                        (int)(heapUsageRatio * 100), maxMemory / (1024 * 1024));
            }
            return;
        }

        try {
            byte[] chunk = new byte[LEAK_SIZE_BYTES];
            for (int i = 0; i < chunk.length; i += 4096) {
                chunk[i] = 1;
            }
            leakContainer.add(chunk);
            long total = leakedBytes.addAndGet(LEAK_SIZE_BYTES);
            log.debug("F5: Leaked {}MB total. Heap {}% full.",
                    total / (1024 * 1024), (int)(heapUsageRatio * 100));

        } catch (OutOfMemoryError e) {
            log.error("F5: Unexpected OOM despite watermark guard. Self-terminating.");
            criticallyFull.set(true);
            active.set(false);
        }
    }

    @PreDestroy
    public void cleanUp() {
        if (active.get()) {
            deactivate();
        }
    }
}
