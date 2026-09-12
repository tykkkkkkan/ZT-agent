/**
 * 中渔天下企业官网 - 首页 Vue3 逻辑
 *
 * 功能：
 *   - SSE 流式 AI 对话 + 打字机效果 + 订单成功卡片
 *   - session_id 持久化（localStorage）
 *   - 历史消息恢复：进入页面时从后端 /api/agent/history/<sid>/ 拉取，跳转后不丢消息
 *   - 新对话：一键重置会话
 */
const { createApp, ref, nextTick, onMounted } = Vue;

createApp({
    setup() {
        // ========== session_id 持久化 ==========
        const SESSION_KEY = 'zyt_session_id';
        const getSessionId = () => {
            let sid = localStorage.getItem(SESSION_KEY);
            if (!sid) {
                sid = Math.random().toString(36).substring(2) + Date.now().toString(36);
                localStorage.setItem(SESSION_KEY, sid);
            }
            return sid;
        };

        // ========== 快捷提问 ==========
        const quickQuestions = ref([
            '钓鲤鱼用什么饵效果最好？',
            '红虫颗粒现在库存还有多少？',
            '批发10箱红虫颗粒怎么报价？',
            '我们支持7天无理由退换货吗？'
        ]);

        // ========== 欢迎消息（历史为空时显示） ==========
        const welcomeMessage = {
            role: 'ai',
            content: '您好，我是「中渔小助」\n中渔天下的专属饵料顾问，全天在线。\n\n我可以帮您：\n· 查产品价格与规格\n· 查库存与到货\n· 算批发报价、下订单\n· 问售后政策\n· 聊钓鱼技巧与季节钓法\n\n试试点击下面的快捷问题，马上开聊～'
        };

        // ========== 未登录提示（AI 对话已纳入登录保护） ==========
        const loginPromptMessage = {
            role: 'ai',
            content: '您好，我是「中渔小助」。\n\n为了记录您的咨询与订单信息，请先登录后再开始对话。\n登录后我可以帮您：\n· 查产品价格与规格\n· 查库存与到货\n· 算批发报价、下订单\n· 问售后政策\n\n点击右上角「登录」即可开始，登录成功后回到本页继续咨询。'
        };

        // 当前是否已登录（对话与历史均需登录态）
        const loginRequired = () => !(window.AUTH && AUTH.isLoggedIn());

        // ========== 聊天状态 ==========
        const messages = ref([welcomeMessage]);
        const inputText = ref('');
        const loading = ref(false);
        const historyLoaded = ref(false);
        const messageBox = ref(null);
        const pendingCard = ref(null);
        const pendingGuide = ref(false);

        // 滚动到底部
        const scrollToBottom = () => {
            nextTick(() => {
                if (messageBox.value) {
                    messageBox.value.scrollTop = messageBox.value.scrollHeight;
                }
            });
        };

        /**
         * 从后端恢复历史消息（跳转/刷新后不丢消息）
         * 未登录时不请求接口，直接展示登录引导（历史接口已纳入登录保护）
         */
        const loadHistory = async () => {
            if (loginRequired()) {
                messages.value = [loginPromptMessage];
                historyLoaded.value = true;
                scrollToBottom();
                return;
            }
            const sid = getSessionId();
            try {
                const resp = await AUTH.authFetch(`/api/agent/history/${sid}/`, {}, { silentAuthFail: true });
                if (resp.status === 401) {
                    messages.value = [loginPromptMessage];
                    historyLoaded.value = true;
                    scrollToBottom();
                    return;
                }
                const data = await resp.json();
                if (data.success && Array.isArray(data.messages) && data.messages.length > 0) {
                    messages.value = data.messages.map(m => ({
                        role: m.role === 'assistant' ? 'ai' : 'user',
                        content: (m.content || '').replace(/\*\*/g, ''),
                    }));
                } else {
                    messages.value = [welcomeMessage];
                }
            } catch (e) {
                messages.value = [welcomeMessage];
            }
            historyLoaded.value = true;
            scrollToBottom();
        };

        /**
         * 新对话：重置会话 ID 并清空当前消息
         */
        const newChat = () => {
            if (loading.value) return;
            const sid = Math.random().toString(36).substring(2) + Date.now().toString(36);
            localStorage.setItem(SESSION_KEY, sid);
            messages.value = [loginRequired() ? loginPromptMessage : welcomeMessage];
            pendingCard.value = null;
            scrollToBottom();
        };

        // 进入页面即加载历史
        onMounted(loadHistory);

        /**
         * 解析 SSE 行：只处理 data: 开头的行
         */
        const parseSSELine = (line) => {
            const trimmed = line.trim();
            if (!trimmed.startsWith('data:')) return null;
            const jsonStr = trimmed.substring(5).trim();
            try {
                return JSON.parse(jsonStr);
            } catch (e) {
                return null;
            }
        };

        /**
         * 强制触发 Vue 响应式更新
         */
        const forceReactivityUpdate = (idx) => {
            if (idx < messages.value.length) {
                const item = messages.value[idx];
                messages.value.splice(idx, 1, item);
            }
        };

        /**
         * 发送消息（SSE 流式版本）
         */
        const sendMessage = async () => {
            const text = inputText.value.trim();
            if (!text || loading.value) return;

            // 登录守卫：未登录 / 登录态失效 → 跳登录页并带回跳参数
            if (!window.AUTH || !(await AUTH.ensureLogin('登录后即可与中渔小助对话'))) return;

            inputText.value = '';

            messages.value.push({ role: 'user', content: text });
            loading.value = true;
            scrollToBottom();

            const aiMsgIdx = messages.value.length;
            messages.value.push({ role: 'ai', content: '', streaming: true });
            scrollToBottom();

            let streamEnded = false;
            pendingCard.value = null;
            pendingGuide.value = false;

            try {
                const sessionId = getSessionId();

                const response = await AUTH.authFetch('/api/agent/chat/', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        message: text,
                        session_id: sessionId,
                    }),
                });

                if (response.status === 401) {
                    messages.value.splice(aiMsgIdx, 1, {
                        role: 'error',
                        content: '登录状态已失效，请重新登录后再试。'
                    });
                    loading.value = false;
                    return;
                }

                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }

                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let buffer = '';

                while (!streamEnded) {
                    const { done, value } = await reader.read();
                    if (done) break;

                    buffer += decoder.decode(value, { stream: true });

                    const parts = buffer.split('\n\n');
                    buffer = parts.pop();

                    for (const part of parts) {
                        if (streamEnded) break;
                        const lines = part.split('\n');
                        for (const line of lines) {
                            if (streamEnded) break;
                            const data = parseSSELine(line);
                            if (!data) continue;

                            // session_id 更新
                            if (data.session_id) {
                                localStorage.setItem(SESSION_KEY, data.session_id);
                            }

                            // 订单成功卡片（兼容保留）
                            if (data.type === 'order_success') {
                                pendingCard.value = {
                                    orderNo: data.order_no || '',
                                };
                                continue;
                            }

                            // 引导转化卡片（去定制服务 / 联系我们）
                            if (data.type === 'guide_card') {
                                pendingGuide.value = true;
                                continue;
                            }

                            // 流结束信号
                            if (data.done) {
                                if (messages.value[aiMsgIdx]) {
                                    messages.value[aiMsgIdx].streaming = false;
                                    if (!messages.value[aiMsgIdx].content) {
                                        messages.value[aiMsgIdx].content = '（未收到回复，请重试）';
                                    }
                                    forceReactivityUpdate(aiMsgIdx);
                                }
                                streamEnded = true;
                                break;
                            }

                            // 文本内容追加
                            if (data.content && messages.value[aiMsgIdx]) {
                                messages.value[aiMsgIdx].content += data.content;
                                forceReactivityUpdate(aiMsgIdx);
                                scrollToBottom();
                            }
                        }
                    }
                }

                // 流结束后，如果有待渲染的卡片，插入到 AI 消息后面
                if (pendingCard.value) {
                    const card = pendingCard.value;
                    nextTick(() => {
                        messages.value.splice(aiMsgIdx + 1, 0, {
                            role: 'card',
                            cardType: 'order_success',
                            orderNo: card.orderNo,
                        });
                        scrollToBottom();
                    });
                }
                // 引导转化卡片（去定制服务 / 联系我们）
                // 优先级1：后端发了 guide_card 事件
                // 优先级2：AI 回复文本含引导词（后端未走工具时的兜底）
                if (pendingGuide.value) {
                    nextTick(() => {
                        messages.value.splice(aiMsgIdx + 1, 0, {
                            role: 'card',
                            cardType: 'guide',
                        });
                        scrollToBottom();
                    });
                } else {
                    const aiText = messages.value[aiMsgIdx] ? (messages.value[aiMsgIdx].content || '') : '';
                    if (/(定制服务|联系我们|提交需求|联系客服)/.test(aiText)) {
                        nextTick(() => {
                            messages.value.splice(aiMsgIdx + 1, 0, {
                                role: 'card',
                                cardType: 'guide',
                            });
                            scrollToBottom();
                        });
                    }
                }
            } catch (err) {
                console.error('SSE 接收失败：', err);
                if (!streamEnded) {
                    messages.value.splice(aiMsgIdx, 1, {
                        role: 'error',
                        content: '连接后端失败，请检查后端服务是否启动。'
                    });
                }
            }

            loading.value = false;
            nextTick(() => {
                const input = document.querySelector('.chat-input');
                if (input) input.focus();
            });
            scrollToBottom();
        };

        const sendQuickQuestion = (question) => {
            if (loading.value) return;
            inputText.value = question;
            sendMessage();
        };

        const goToPage = (url) => {
            window.location.href = url;
        };

        return {
            quickQuestions,
            messages,
            inputText,
            loading,
            historyLoaded,
            messageBox,
            sendMessage,
            sendQuickQuestion,
            goToPage,
            newChat,
        };
    },
}).mount('#app');
