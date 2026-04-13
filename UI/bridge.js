// bridge.js - 修复版

// robots.txt 异步回调：由 Python request_preview_robots_txt 完成后 evaluate_js 调用
window.__robotsPreviewPending = window.__robotsPreviewPending || {};
window.__resolveRobotsPreview = function (payload) {
    if (!payload || !payload.id) return;
    const pending = window.__robotsPreviewPending[payload.id];
    delete window.__robotsPreviewPending[payload.id];
    if (!pending) return;
    if (payload.ok) pending.resolve(payload.content != null ? String(payload.content) : "");
    else pending.reject(new Error(payload.message || "robots 抓取失败"));
};

// 1. 定义核心 UI 交互对象
window.bridge = {
    // === [修复 1] 新增 log 方法，解决 TypeError 报错 ===
    log: function(message, level="info") {
        const logBox = document.getElementById('log-content');
        // 构造一个标准日志对象，复用现有的渲染逻辑
        const payload = {
            type: 'log',
            message: message,
            level: level,
            time: new Date().toLocaleTimeString()
        };
        
        if (logBox) {
            this.appendSingleLog(payload, logBox);
        } else {
            console.log(`[无界面日志] ${level}: ${message}`);
        }
    },

    // === [关键修复] 新增 check_robots 接口 ===
    // 作用：将下载任务转发给 Python，绕过浏览器同源策略(CORS)导致的 "Failed to fetch"
    checkRobots: async function(url) {
        if (window.pywebview && window.pywebview.api) {
            try {
                const api = window.pywebview.api;
                if (typeof api.request_preview_robots_txt !== "function") {
                    throw new Error("后端未实现 request_preview_robots_txt");
                }
                const requestId =
                    "robots_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 10);
                const p = new Promise(function (resolve, reject) {
                    window.__robotsPreviewPending[requestId] = { resolve: resolve, reject: reject };
                });
                const ack = await api.request_preview_robots_txt(url, requestId);
                if (!ack || !ack.accepted) {
                    delete window.__robotsPreviewPending[requestId];
                    throw new Error((ack && ack.message) || "请求未被后端接受");
                }
                return await p;
            } catch (err) {
                console.error("[Bridge] 调用 Python 失败:", err);
                throw err;
            }
        } else {
            // 本地调试模式（无 Python 环境时）返回模拟数据
            console.warn("[Bridge] 未连接 Python，返回模拟数据");
            return "User-agent: *\nSitemap: https://mock-data.com/sitemap.xml";
        }
    },

    onEvent: function(payload) {
        // 如果收到的是数组（批量日志），进入高性能批量渲染模式
        if (Array.isArray(payload)) {
            this.handleBulkEvents(payload);
            return;
        }

        // 处理单条数据
        const logBox = document.getElementById('log-content');
        if (!logBox) return;

        if (payload.type === 'log') {
            this.appendSingleLog(payload, logBox);
        } else if (payload.type === 'stats') {
            if (window.updateStatsUI) window.updateStatsUI(payload.data);
        } else if (payload.type === 'status_update') {
            this.updateStatusUI(payload.status);
        }
    },

    // --- 高性能批量处理引擎 ---
    handleBulkEvents: function(payloads) {
        const logBox = document.getElementById('log-content');
        if (!logBox) return;

        const fragment = document.createDocumentFragment();
        let hasLogs = false;
        let latestStats = null;
        let latestStatus = null;

        payloads.forEach(p => {
            if (p.type === 'log') {
                fragment.appendChild(this.createLogNode(p));
                hasLogs = true;
            } else if (p.type === 'stats') {
                latestStats = p;
            } else if (p.type === 'status_update') {
                latestStatus = p;
            }
        });

        if (hasLogs) {
            const isAtBottom = (logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight) < 50;
            logBox.appendChild(fragment);

            // 限制日志条数，防止内存溢出
            while (logBox.childElementCount > 1000) {
                logBox.removeChild(logBox.firstChild);
            }

            if (isAtBottom) {
                logBox.scrollTop = logBox.scrollHeight;
            }
        }

        if (latestStats && window.updateStatsUI) window.updateStatsUI(latestStats.data);
        if (latestStatus) this.updateStatusUI(latestStatus.status);
    },

    // --- 辅助方法 ---
    createLogNode: function(payload) {
        const div = document.createElement('div');
        div.className = `log-entry ${payload.level || 'info'}`;
        const timeStr = payload.time || new Date().toLocaleTimeString();
        div.innerHTML = `<span class="log-time">[${timeStr}]</span> <span class="log-msg">${payload.message}</span>`;
        return div;
    },

    appendSingleLog: function(payload, logBox) {
        const isAtBottom = (logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight) < 50;
        logBox.appendChild(this.createLogNode(payload));
        
        while (logBox.childElementCount > 1000) {
            logBox.removeChild(logBox.firstChild);
        }
        if (isAtBottom) logBox.scrollTop = logBox.scrollHeight;
    },

    updateStatusUI: function(status) {
        // 更新左侧的状态徽章 (如果有的话)
        const statusBadge = document.getElementById('status-badge');
        if(statusBadge) {
            statusBadge.textContent = status === 'running' ? '运行中' : '已停止';
            statusBadge.className = status === 'running' ? 'status-badge running' : 'status-badge stopped';
        }
    }
};

// ============================================================
// Crawlee 控制接口
// ============================================================

/**
 * 启动爬虫
 */
window.run_crawler = async function(config) {
    console.log("[Bridge] 接收到启动指令，配置:", config);
    
    // 检查后端连接
    if (window.pywebview && window.pywebview.api) {
        try {
            window.bridge.log("正在初始化爬虫引擎...", "info");
            const payload =
                typeof config === "string" ? config : JSON.stringify(config);
            const result = await window.pywebview.api.start_task(payload);
            if (result && result.success === false) {
                throw new Error(result.message || "启动被拒绝");
            }
        } catch (error) {
            console.error("Python 调用失败:", error);
            window.bridge.log("启动失败: " + error, "error");
            
            // 如果报错了，手动把按钮恢复原状（假设你有 global function resetBtn）
            if (window.resetStartButton) window.resetStartButton(); 
        }
    } else {
        console.warn("未检测到 pywebview，仅本地测试。");
        window.bridge.log("【演示模式】未连接 Python 后端，无法实际抓取。", "warning");
        if (window.resetStartButton) window.resetStartButton();
    }
};

/**
 * 停止爬虫
 */
window.stop_crawler = async function() {
    console.log("[Bridge] 接收到停止指令");
    if (window.pywebview && window.pywebview.api) {
        try {
            await window.pywebview.api.stop_task();
            window.bridge.log("正在发送停止信号...", "warning");
        } catch (error) {
            window.bridge.log("停止指令发送失败: " + error, "error");
        }
    }
};

/**
 * 供 Python 反向调用的日志接口
 * Python 代码: window.evaluate_js("window.update_log('消息', 'info')")
 */
window.update_log = function(message, type="info") {
    // 这里的 window.bridge.log 现在已经存在了，不会再报错
    if (window.bridge) {
        window.bridge.log(message, type);
    }
};

// ============================================================
// === [修复 2] 监听后端就绪事件 (解决红灯问题) ===
// ============================================================
window.addEventListener('pywebviewready', function() {
    console.log("%c Python Backend Ready ", "background: #2ecc71; color: white; padding: 4px; border-radius: 4px;");
    
    // 1. 打印一条系统日志
    window.bridge.log("Python 后端核心已连接", "success");

    // 2. 改变右下角红灯状态 (假设红灯所在的 DOM ID 是 status-light 或类似的 footer 元素)
    // 根据你的 UI 代码，这里可能需要调整 ID
    const footerStatus = document.querySelector('.status-indicator') || document.getElementById('connection-status');
    const statusText = document.querySelector('.status-text');

    if (footerStatus) {
        footerStatus.style.backgroundColor = '#2ecc71'; // 变绿
        footerStatus.style.boxShadow = '0 0 10px #2ecc71';
    }
    
    if (statusText) {
        statusText.textContent = "后端已连接";
        statusText.style.color = "#2ecc71";
    }
});