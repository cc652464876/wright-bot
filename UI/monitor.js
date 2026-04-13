/* ui/monitor.js */

/**
 * 工具函数：将秒数格式化为 HH.MM.SS (例如 65秒 -> 00.01.05)
 */
function formatTime(seconds) {
    if (!seconds) return "00.00.00";
    const h = Math.floor(seconds / 3600).toString().padStart(2, '0');
    const m = Math.floor((seconds % 3600) / 60).toString().padStart(2, '0');
    const s = Math.floor(seconds % 60).toString().padStart(2, '0');
    return `${h}.${m}.${s}`;
}

/**
 * 工具函数：格式化均时 (保留3位小数，例如 0.123)
 */
function formatAvg(seconds) {
    if (!seconds) return "000";
    return seconds.toFixed(1);
}

/**
 * 辅助函数：控制一组 DOM 元素的显示/隐藏
 * @param {Array} ids - 需要控制的 DOM ID 数组
 * @param {Boolean} show - true 显示，false 隐藏
 */
function toggleVisibility(ids, show) {
    ids.forEach(id => {
        const el = document.getElementById(id);
        if (el) {
            show ? el.classList.remove('hidden') : el.classList.add('hidden');
        }
    });
}

// === 主逻辑 ===
window.addEventListener('DOMContentLoaded', () => {
    
    // 1. 日志轮询 (1秒一次)
    // 负责从后端拉取文本日志，并交给 bridge.js 显示在下方黑框里
    setInterval(async () => {
        if (window.pywebview?.api?.fetch_logs) {
            try {
                const logs = await window.pywebview.api.fetch_logs();
                if (logs && logs.length > 0 && window.bridge?.onEvent) {
                    logs.forEach(payload => window.bridge.onEvent(payload));
                }
            } catch (e) { /* 忽略超时 */ }
        }
    }, 1000);

    // 2. 📊 仪表盘数据轮询 (0.5秒一次)
    // 负责让上面的数码管动起来
    setInterval(async () => {
        // 确保 API 已就绪
        if (window.pywebview?.api?.fetch_statistics) {
            try {
                const data = await window.pywebview.api.fetch_statistics();
                
                // 如果后端返回空，说明没在运行或出错，跳过更新
                if (!data || Object.keys(data).length === 0) return;

                // --- Group 1: 任务进度 (链接数量) ---
                document.getElementById('val-req-total').innerText   = String(data.requests_total).padStart(3, '0');
                document.getElementById('val-req-success').innerText = String(data.requests_finished).padStart(3, '0');
                
                // [红色警报] 失败请求
                const failCount = data.requests_failed || 0;
                document.getElementById('val-req-fail').innerText = String(failCount).padStart(3, '0');
                toggleVisibility(
                    ['val-req-fail', 'label-req-fail', 'label-sep-req-fail', 'val-sep-req-fail'], 
                    failCount > 0
                );

                // --- Group 2: 采集数量 (文件) ---
                document.getElementById('val-file-found').innerText      = String(data.files_found).padStart(3, '0');
                document.getElementById('val-file-downloaded').innerText = String(data.files_downloaded).padStart(3, '0');
                document.getElementById('val-file-current').innerText    = String(data.files_active).padStart(3, '0');

                // --- Group 3: 时效分布 (秒) ---
                document.getElementById('val-time-runtime').innerText     = formatTime(data.crawler_runtime);
                document.getElementById('val-time-success-avg').innerText = formatAvg(data.avg_success_duration);
                
                // [红色警报] 失败均时
                const failAvg = data.avg_failed_duration || 0;
                document.getElementById('val-time-fail-avg').innerText = formatAvg(failAvg);
                toggleVisibility(
                    ['val-time-fail-avg', 'label-time-fail', 'label-sep-time-fail', 'val-sep-time-fail'], 
                    failAvg > 0
                );

                // --- Group 4: 负载状态 (链接/分钟) ---
                document.getElementById('val-health-rpm').innerText = Math.round(data.requests_per_minute || 0);

                // [橙色警报] 重试频率
                const retryCount = data.retry_count || 0;
                document.getElementById('val-health-retry').innerText = String(retryCount).padStart(3, '0');
                toggleVisibility(
                    ['val-health-retry', 'label-health-retry', 'label-sep-health-retry', 'val-sep-health-retry'], 
                    retryCount > 0
                );

                // [红色警报] 失败频率
                const failRate = Math.round(data.failed_per_minute || 0);
                document.getElementById('val-health-fail').innerText = String(failRate).padStart(3, '0');
                toggleVisibility(
                    ['val-health-fail', 'label-health-fail', 'label-sep-health-fail', 'val-sep-health-fail'], 
                    failRate > 0
                );

            } catch (e) {
                console.warn("Dashboard update error:", e);
            }
        }
    }, 500); // 0.5s 刷新率，让数字跳动更跟手
});