/* UI/canary.js — 金丝雀：fetch_canary_dashboard + fetch_canary_logs（独立缓冲区，不经过 fetch_logs） */

(function () {
    'use strict';

    const Q_KEYS = ['network', 'identity', 'hardware', 'combat'];

    const DOT_CLASS = {
        pass: 'dot-pass',
        warn: 'dot-warn',
        fail: 'dot-fail',
        idle: 'dot-idle',
        loading: 'dot-loading',
    };

    function dotClassForState(state) {
        const s = (state || 'idle').toLowerCase();
        return DOT_CLASS[s] ? DOT_CLASS[s] : DOT_CLASS.idle;
    }

    function renderQuadrants(quadrants) {
        if (!quadrants || typeof quadrants !== 'object') return;
        Q_KEYS.forEach((key) => {
            const body = document.getElementById('q-body-' + key);
            if (!body) return;
            const items = quadrants[key];
            if (!Array.isArray(items)) {
                body.innerHTML = '';
                return;
            }
            body.innerHTML = '';
            items.forEach((item) => {
                const row = document.createElement('div');
                row.className = 'check-row';
                row.dataset.id = item.id || '';

                const dot = document.createElement('span');
                dot.className = 'status-dot ' + dotClassForState(item.state);
                dot.setAttribute('aria-label', item.state || 'idle');

                const main = document.createElement('div');
                main.className = 'check-main';

                const lab = document.createElement('span');
                lab.className = 'check-label';
                lab.textContent = item.label || '';

                const desc = document.createElement('span');
                desc.className = 'check-desc';
                desc.textContent = item.desc || '';

                main.appendChild(lab);
                main.appendChild(desc);
                row.appendChild(dot);
                row.appendChild(main);
                body.appendChild(row);
            });
        });
    }

    function applyDashboard(data) {
        if (!data || typeof data !== 'object') return;

        const btn = document.getElementById('btn-canary-run');
        const picker = document.getElementById('canary-stealth-engine');

        const sys = (data.system_state || 'idle').toLowerCase();

        if (picker && data.current_engine && picker.value !== data.current_engine) {
            picker.value = data.current_engine;
        }

        if (btn) {
            const locked = sys === 'locked';
            const running = sys === 'running';
            btn.disabled = locked || running;
            if (running) {
                btn.textContent = '检测中…';
                btn.removeAttribute('title');
            } else if (locked) {
                btn.textContent = '运行体检';
                btn.title = '主控任务运行中，无法进行金丝雀体检';
            } else {
                btn.textContent = '运行体检';
                btn.removeAttribute('title');
            }
        }

        renderQuadrants(data.quadrants);
    }

    function mockDashboard() {
        return {
            system_state: 'idle',
            current_engine: 'chromium',
            progress_percent: 0,
            quadrants: {
                network: [
                    { id: 'tls_ja3', label: 'TLS / JA3 指纹校验', state: 'idle', desc: '本地演示：未连接 Python' },
                    { id: 'http_headers', label: 'HTTP 报文与 IP 连通性', state: 'idle', desc: '—' },
                    { id: 'webrtc_leak', label: 'WebRTC 真实 IP 泄露', state: 'idle', desc: '—' },
                ],
                identity: [
                    { id: 'identity_locale', label: '综合身份与语言时区', state: 'idle', desc: '—' },
                    { id: 'viewport_fit', label: '视口逻辑与物理尺寸对齐', state: 'idle', desc: '—' },
                ],
                hardware: [
                    { id: 'webgl_vendor', label: 'WebGL 厂商与渲染引擎', state: 'idle', desc: '—' },
                    { id: 'canvas_audio', label: '画布与音频哈希噪点', state: 'idle', desc: '—' },
                ],
                combat: [
                    { id: 'cf_shield', label: 'Cloudflare 隐形质询 (5s盾)', state: 'idle', desc: '—' },
                    { id: 'cdp_automation', label: 'CDP 协议与自动化漏洞', state: 'idle', desc: '—' },
                    { id: 'behavior_score', label: '仿生行为与轨迹评分', state: 'idle', desc: '—' },
                ],
            },
        };
    }

    async function pollDashboard() {
        const api = window.pywebview && window.pywebview.api;
        if (api && typeof api.fetch_canary_dashboard === 'function') {
            try {
                const data = await api.fetch_canary_dashboard();
                applyDashboard(data);
                return;
            } catch (e) {
                console.warn('[Canary] fetch_canary_dashboard:', e);
            }
        }
        applyDashboard(mockDashboard());
    }

    /**
     * 仅渲染 fetch_canary_logs 返回的条目（独立通道，不调用全局 fetch_logs）。
     */
    function renderCanaryLogs(entries) {
        const logBox = document.getElementById('canary-log-content');
        if (!logBox) return;

        if (!entries || entries.length === 0) {
            logBox.innerHTML = '<div class="canary-log-placeholder">--- 金丝雀专属日志（无条目）---</div>';
            return;
        }

        const chronological = [...entries].reverse();
        logBox.innerHTML = '';
        chronological.forEach((raw) => {
            const u = String(raw.level || 'INFO').toUpperCase();
            let cssLevel = 'info';
            if (u === 'WARNING' || u === 'WARN') cssLevel = 'warning';
            else if (u === 'ERROR' || u === 'CRITICAL') cssLevel = 'error';
            else if (u === 'DEBUG' || u === 'TRACE') cssLevel = 'info';
            const payload = {
                type: 'log',
                time: raw.time || '',
                level: cssLevel,
                message: raw.message || '',
            };
            if (window.bridge && typeof window.bridge.createLogNode === 'function') {
                logBox.appendChild(window.bridge.createLogNode(payload));
            } else {
                const div = document.createElement('div');
                div.className = 'log-entry info';
                div.innerHTML = '<span class="log-time">[' + payload.time + ']</span> <span class="log-msg">' +
                    (payload.message || '') + '</span>';
                logBox.appendChild(div);
            }
        });
    }

    async function pollCanaryLogs() {
        const api = window.pywebview && window.pywebview.api;
        if (!api || typeof api.fetch_canary_logs !== 'function') return;
        try {
            const logs = await api.fetch_canary_logs(200);
            renderCanaryLogs(logs);
        } catch (e) { /* ignore */ }
    }

    async function syncStealthFromBackend() {
        const api = window.pywebview && window.pywebview.api;
        const picker = document.getElementById('canary-stealth-engine');
        if (!picker || !api || typeof api.get_stealth_engine !== 'function') return;
        try {
            const eng = await api.get_stealth_engine();
            if (eng && picker.value !== eng) {
                picker.value = eng;
            }
        } catch (e) { /* ignore */ }
    }

    window.addEventListener('DOMContentLoaded', () => {
        const picker = document.getElementById('canary-stealth-engine');
        const btn = document.getElementById('btn-canary-run');

        if (picker) {
            picker.addEventListener('change', async () => {
                const api = window.pywebview && window.pywebview.api;
                if (!api || typeof api.set_stealth_engine !== 'function') return;
                try {
                    await api.set_stealth_engine(picker.value);
                } catch (e) {
                    console.warn('[Canary] set_stealth_engine:', e);
                }
            });
        }

        if (btn) {
            btn.addEventListener('click', async () => {
                const api = window.pywebview && window.pywebview.api;
                if (!api || typeof api.run_canary_checkup !== 'function') {
                    return;
                }
                try {
                    await api.run_canary_checkup();
                    await pollCanaryLogs();
                } catch (e) {
                    if (api.append_canary_log) {
                        try {
                            await api.append_canary_log('[金丝雀] 调用失败: ' + e, 'ERROR');
                        } catch (e2) { /* ignore */ }
                    }
                    await pollCanaryLogs();
                }
            });
        }

        setInterval(pollCanaryLogs, 1000);
        setInterval(pollDashboard, 800);

        const start = () => {
            syncStealthFromBackend();
            pollDashboard();
            pollCanaryLogs();
        };
        if (window.pywebview && window.pywebview.api) {
            start();
        } else {
            window.addEventListener('pywebviewready', start);
            start();
        }
    });
})();
