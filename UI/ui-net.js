/* ==========================================
 * ui-net.js (最终修复版 - 解决图标消失与语法错误)
 * ========================================== */

// 1. 网速仪表盘更新 (智能进位版)
window.updateNetSpeedUI = function(upSpeed, downSpeed) {
    const upElement = document.getElementById('up-speed');
    const downElement = document.getElementById('down-speed');
    const upUnitElement = document.getElementById('up-unit');
    const downUnitElement = document.getElementById('down-unit');
    
    if (!upElement || !downElement) return;

    // 核心转换器：假设后端传入的是 KB/s，超过 1024 自动转换为 MB/s
    const formatSpeed = (speedInKB) => {
        const num = parseFloat(speedInKB || 0);
        if (num >= 1024) {
            return { value: (num / 1024).toFixed(1), unit: 'M' };
        } else {
            return { value: num.toFixed(1), unit: 'K' };
        }
    };

    const upData = formatSpeed(upSpeed);
    const downData = formatSpeed(downSpeed);

    // 渲染数字
    upElement.textContent = upData.value;
    downElement.textContent = downData.value;

    // 动态渲染单位 (M 或 K)
    if (upUnitElement) upUnitElement.textContent = upData.unit;
    if (downUnitElement) downUnitElement.textContent = downData.unit;
};

// 2. 核心网络连通性更新 (双路独立监测 - 动态信号增强版)
window.updateNetStatusUI = function(cnPing, glbPing) {
    const cnBox = document.getElementById('icon-cn-box');
    const glbBox = document.getElementById('icon-glb-box');
    
    if (!cnBox || !glbBox) return;

    // 新增 prefix 参数，用来区分海内海外
    const renderSignal = (container, ping, prefix) => {
        let iconName = 'link-off';
        let colorClass = 'net-disconnected'; 

        if (ping >= 0) {
            colorClass = 'net-blue'; 
            if (ping <= 150) iconName = 'confidence-four';
            else if (ping <= 350) iconName = 'confidence-three';
            else if (ping <= 800) iconName = 'confidence-two';
            else iconName = 'confidence-one';
        }
        
        // 组装精确提示文本
        const titleText = ping >= 0 ? `${prefix}网络延迟 ${ping}ms` : `${prefix}网络断开`;
        
        // ⭐️ 核心修复：把 title 直接赋给原生的父级 div 容器
        container.title = titleText;
        
        // 渲染图标（移除了标签里的 title 属性）
        container.innerHTML = `<sp-icon-${iconName} class="${colorClass}"></sp-icon-${iconName}>`;
    };

    // 传入前缀进行渲染
    renderSignal(cnBox, cnPing, "海内");
    renderSignal(glbBox, glbPing, "海外");
};

console.log("📶 UI-NET: 双路监控逻辑已成功修复并加载");