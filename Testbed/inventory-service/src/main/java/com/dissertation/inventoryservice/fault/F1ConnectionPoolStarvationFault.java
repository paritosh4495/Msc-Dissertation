package com.dissertation.inventoryservice.fault;

import com.zaxxer.hikari.HikariDataSource;
import jakarta.annotation.PostConstruct;
import jakarta.annotation.PreDestroy;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Component;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;

@Component
@RequiredArgsConstructor
@Slf4j
public class F1ConnectionPoolStarvationFault implements Fault {

    private final FaultRegistry registry;
    private final DataSource dataSource;
    private final AtomicBoolean active = new AtomicBoolean(false);

    private final List<Connection> heldConnections = new CopyOnWriteArrayList<>();
    private final ExecutorService acquisitionExecutor = Executors.newFixedThreadPool(10);
    private  ScheduledExecutorService backgroundStealer;

    @PostConstruct
    public void init() {
        registry.register(this);
    }

    @Override
    public String getId() {
        return "f1";
    }

    @Override
    public String getDescription() {
        return "Starves the database connection pool by holding all connections";
    }

    @Override
    public boolean isActive() {
        return active.get();
    }

    @Override
    public synchronized void activate() {
        if (active.get()) return;

        if (!(dataSource instanceof HikariDataSource hikariDataSource)) {
            throw new IllegalStateException("DataSource is not a HikariDataSource");
        }

        int maxPoolSize = hikariDataSource.getHikariConfigMXBean().getMaximumPoolSize();
        log.info("F1: Activating starvation. Max pool size is {}", maxPoolSize);
        active.set(true);

        // Background task to continuously acquire connections as they become available
        backgroundStealer = Executors.newSingleThreadScheduledExecutor();
        backgroundStealer.scheduleAtFixedRate(()->{
            if(heldConnections.size()<maxPoolSize) {
                Connection conn = tryGetConnection(100);
                if (conn != null) {
                    heldConnections.add(conn);
                    log.info("F1 Leak: Stole a connection. Now holding {}/{} ", heldConnections.size(), maxPoolSize);
                } else {
                    log.debug("F1: Pool is fully starved");
                }
            }
            }, 0, 50, TimeUnit.MILLISECONDS);
    }

    @Override
    public synchronized void deactivate() {
        if(!active.get()) return;

        // Stop the acquisition task
        if(backgroundStealer!=null) {
            backgroundStealer.shutdownNow();

            try {
                backgroundStealer.awaitTermination(200, TimeUnit.MILLISECONDS);

            }catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            backgroundStealer = null;
        }

        // Return all held connections to the pool
        log.info("Deactivating F1: Releasing {} connections", heldConnections.size());
        heldConnections.forEach(this::closeQuietly);
        heldConnections.clear();
        
        active.set(false);
    }

    private Connection tryGetConnection(long timeoutMs) {
       CompletableFuture<Connection> future = CompletableFuture.supplyAsync(()->{
           try {
               return dataSource.getConnection();
           }catch (SQLException e) {
              return null;
           }
       }, acquisitionExecutor);

       try {
           return future.get(timeoutMs,TimeUnit.MILLISECONDS);
       }catch (TimeoutException e){
           // Prevent leaks if a connection is acquired after the timeout
           future.thenAccept(this::closeQuietly);
           return null;
       } catch (InterruptedException | ExecutionException e) {
           future.thenAccept(this::closeQuietly);
           return null;
       }
    }

    private void closeQuietly(Connection conn) {
        try {
            if (conn != null && !conn.isClosed()) {
                conn.close();
            }
        } catch (SQLException e) {
            log.warn("F1: Error closing connection: {}", e.getMessage());
        }
    }

    @PreDestroy
    public void cleanUp(){
        acquisitionExecutor.shutdownNow();
        if(backgroundStealer!=null && !backgroundStealer.isShutdown()) {
            backgroundStealer.shutdownNow();
        }
    }
}
