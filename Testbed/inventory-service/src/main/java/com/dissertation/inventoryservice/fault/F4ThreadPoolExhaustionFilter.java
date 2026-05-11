package com.dissertation.inventoryservice.fault;

import jakarta.servlet.*;
import jakarta.servlet.http.HttpServletRequest;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;

import java.io.IOException;

@Component
@Order(1) // Run early to capture requests before other processing
@RequiredArgsConstructor
@Slf4j
public class F4ThreadPoolExhaustionFilter implements Filter {

    private final F4ThreadPoolExhaustionFault f4Fault;

    @Override
    public void doFilter(ServletRequest request, ServletResponse response, FilterChain chain) 
            throws IOException, ServletException {
        
        if (request instanceof HttpServletRequest httpRequest) {
            String path = httpRequest.getRequestURI();
            
            // Trap API requests to simulate thread pool exhaustion
            if (path.startsWith("/api/") && f4Fault.isActive()) {
                boolean wasTrapped = f4Fault.tryBlock();
                
                if (wasTrapped) {
                    log.debug("F4: Thread released from trap for path: {}", path);
                } else if (f4Fault.isActive()) {
                    // Requests proceed if the trap is at capacity
                    log.info("F4: Request allowed to proceed (trap capacity reached) for path: {}", path);
                }
            }
        }
        
        chain.doFilter(request, response);
    }
}
