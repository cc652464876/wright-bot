/* =========================================================================
 * 文件名: ui-view.js
 * 核心职责: 界面特效与 DOM 魔术师 (纯视觉与交互处理)
 * 作用说明: 
 * 1. 控制动态视图的切换（全站、地图、搜索模式）。
 * 2. 爬虫引擎的智能推荐与下拉框自动跳转。
 * 3. 底部滑块与输入框的数值双向绑定。
 * 4. 处理动态增删目标网址行（LINK 按钮与垃圾桶的事件委托）。
 * 5. Sitemap 智能检索的网络请求与 UI 反馈。
 * ========================================================================= */

document.addEventListener('DOMContentLoaded', () => {
    console.log(">> UI View Module Loaded");

    // ==========================================
    // 1. 辅助：滑块与输入框的双向绑定 (底部过滤器)
    // ==========================================
    function bindControl(sliderId, inputId) {
        const slider = document.getElementById(sliderId);
        const input = document.getElementById(inputId);
        if (slider && input) {
            slider.addEventListener('input', (e) => { input.value = e.target.value; });
            input.addEventListener('change', (e) => { 
                let val = parseFloat(e.target.value);
                if (val < slider.min) val = slider.min;
                if (val > slider.max) val = slider.max;
                slider.value = val;
                input.value = val;
            });
        }
    }

    bindControl('s-file', 'i-file');
    bindControl('s-page', 'i-page');
    bindControl('s-img', 'i-img');
    bindControl('s-px', 'i-px');

    // ==========================================
    // 2. 策略 -> 视图切换 & 引擎推荐逻辑
    // ==========================================
    const strategySelect = document.getElementById('strategy-select');
    const engineSelect = document.getElementById('engine-select');
    
    const viewFull = document.getElementById('view-full');
    const viewSitemap = document.getElementById('view-sitemap');
    const viewSearch = document.getElementById('view-search');

    const viewMap = {
        'full': viewFull,
        'sitemap': viewSitemap,
        'google': viewSearch,
        'bing': viewSearch,
        'duckduckgo': viewSearch
    };

    function updateUIByStrategy(strategyVal) {
        // A. 切换视图显示：先隐藏全部，再显示目标
        [viewFull, viewSitemap, viewSearch].forEach(el => {
            if (el) el.classList.add('hidden');
        });
        
        const targetView = viewMap[strategyVal];
        if (targetView) targetView.classList.remove('hidden');

        // B. 爬虫引擎自动跳转逻辑
        const recommendationMap = {
            'full': 'playwright',
            'sitemap': 'beautifulsoup', 
            'google': 'playwright',
            'bing': 'playwright',
            'duckduckgo': 'playwright'
        };

        const recommendedEngine = recommendationMap[strategyVal];
        if (recommendedEngine && engineSelect) {
            engineSelect.value = recommendedEngine;
            console.log(`[UI View] 策略切换为: ${strategyVal} -> 引擎自动跳转为: ${recommendedEngine}`);
        }
    }

    if (strategySelect) {
        strategySelect.addEventListener('change', (e) => {
            updateUIByStrategy(e.target.value);
        });
        // 页面初始化时执行一次，确保初始状态正确
        updateUIByStrategy(strategySelect.value);
    }

    // ==========================================
    // 3. 强制初始化下拉框默认值 (解决组件偶尔加载空白Bug)
    // ==========================================
    setTimeout(() => {
        const defaultSettings = [
            { id: 'max-concurrency', val: 'auto' },
            { id: 'stealth-engine', val: 'chromium' },
            { id: 'engine-select', val: 'playwright' },
            { id: 'strategy-select', val: 'full' } 
        ];

        defaultSettings.forEach(setting => {
            const el = document.getElementById(setting.id);
            if (el && !el.value) {
                el.value = setting.val;
            }
        });
    }, 200);

    // ==========================================
    // 4. 动态行交互 (LINK 按钮添加 与 垃圾桶删除)
    // ==========================================
    const viewContainer = document.querySelector('.dynamic-view-container');
    
    // A. 垃圾桶删除逻辑 (使用事件委托机制，支持动态添加的元素)
    if (viewContainer) {
        viewContainer.addEventListener('click', (e) => {
            const trashBtn = e.target.closest('.trash-btn');
            if (trashBtn) {
                const row = trashBtn.closest('.input-row');
                if (row) {
                    // 添加一个小动画效果再删除
                    row.style.opacity = '0';
                    setTimeout(() => row.remove(), 200);
                }
            }
        });
    }

    // B. LINK 按钮添加新行逻辑
    const btnAddLink = Array.from(document.querySelectorAll('sp-button')).find(btn => btn.innerText.includes('LINK'));
    
    if (btnAddLink) {
        btnAddLink.addEventListener('click', () => {
            let activeView = null;
            if (viewFull && !viewFull.classList.contains('hidden')) activeView = viewFull;
            else if (viewSearch && !viewSearch.classList.contains('hidden')) activeView = viewSearch;
            else if (viewSitemap && !viewSitemap.classList.contains('hidden')) activeView = viewSitemap;

            if (activeView) {
                const newRow = document.createElement('div');
                newRow.className = 'input-row';
                const commonInput = `<sp-textfield placeholder="请输入目标网址..." style="flex-grow: 1;"></sp-textfield>`;
                const commonTrash = `<div class="square-btn trash-btn"><sp-icon-delete></sp-icon-delete></div>`;

                if (activeView === viewSitemap) {
                    // 地图采集模式特有结构
                    newRow.innerHTML = `${commonInput} <button class="smart-btn">智能检索</button> ${commonTrash}`;
                } else {
                    // 标准结构
                    newRow.innerHTML = `${commonInput} ${commonTrash}`;
                }
                activeView.appendChild(newRow);
            }
        });
    }

    // ==========================================
    // 5. Sitemap 智能检索逻辑 (Robots.txt 扫描)
    // ==========================================
    if (viewContainer) {
        viewContainer.addEventListener('click', async (e) => {
            // 事件委托处理所有的 "智能检索" 按钮
            const btnSmartSearch = e.target.closest('.smart-btn');
            if (!btnSmartSearch) return;

            const inputSitemap = btnSmartSearch.parentElement.querySelector('sp-textfield');
            if (!inputSitemap) return;

            let rawUrl = inputSitemap.value.trim();
            if (!rawUrl) { alert("请输入网址"); return; }

            // 补全协议并拼接 robots.txt
            if (!/^https?:\/\//i.test(rawUrl)) {
                rawUrl = 'https://' + rawUrl;
            }
            const robotsUrl = rawUrl.replace(/\/$/, "") + '/robots.txt';

            // UI 状态变为检索中
            const originalBtnText = btnSmartSearch.innerText;
            btnSmartSearch.innerText = "检索中...";
            btnSmartSearch.disabled = true;

            try {
                console.log(`[Smart Search] Requesting Python to fetch: ${robotsUrl}`);
                // 🟢 新代码: 通过 bridge 调用 Python
                // 注意：这里我们调用刚才在 bridge.js 里写好的方法
                const text = await window.bridge.checkRobots(robotsUrl);

                // 正则匹配 Sitemap
                const match = text.match(/Sitemap:\s*(https?:\/\/[^\s\r\n]+)/i);

                if (match && match[1]) {
                    inputSitemap.value = `${match[1]}_检索成功！`;
                } else {
                    inputSitemap.value = `未在 robots.txt 中找到 Sitemap 字段`;
                }
            } catch (error) {
                console.error(error);
                inputSitemap.value = `未找到 robots.txt (错误: ${error.message})`;
            } finally {
                // 恢复按钮状态
                btnSmartSearch.innerText = originalBtnText;
                btnSmartSearch.disabled = false;
            }
        });
    }
});