const vscode = acquireVsCodeApi();

// UI Elements
const form = document.getElementById('chat-form');
const input = document.getElementById('prompt');
const messages = document.getElementById('messages');
const statusIndicator = document.getElementById('status-indicator');
const statusText = document.getElementById('status-text');
const taskPanel = document.getElementById('task-panel');
const taskProgressBar = document.getElementById('task-progress-bar');
const taskProgressText = document.getElementById('task-progress-text');
const taskDetails = document.getElementById('task-details');
const sendButton = document.getElementById('send-button');
const historyList = document.getElementById('history-list');

// State
let currentMessageElement = null;
let currentMessageContent = null;
let isStreaming = false;

// Simple Markdown Parser (Bold, Code Blocks, Inline Code, Newlines)
function parseMarkdown(text) {
    if (!text) return '';
    let html = text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
    
    // Code blocks
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
    // Inline code
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    // Bold
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    // Newlines
    html = html.replace(/\n/g, '<br/>');
    
    return html;
}

// Auto-resize textarea
input.addEventListener('input', function() {
    this.style.height = 'auto';
    this.style.height = (this.scrollHeight) + 'px';
});

// Handle form submission
form.addEventListener('submit', event => {
    event.preventDefault();
    const prompt = input.value.trim();
    if (prompt && !isStreaming) {
        // Add User Message
        addMessage('user', prompt);
        
        // Disable input
        isStreaming = true;
        input.disabled = true;
        sendButton.disabled = true;
        
        // Add to history
        const li = document.createElement('li');
        li.className = 'history-item';
        li.textContent = prompt;
        historyList.appendChild(li);
        
        // Prepare UI for Pulse Response
        statusIndicator.className = 'status-indicator thinking';
        statusText.textContent = 'Pulse is thinking...';
        
        currentMessageElement = createMessageBubble('pulse');
        messages.appendChild(currentMessageElement);
        currentMessageContent = currentMessageElement.querySelector('.message-content');
        
        // Send to extension
        vscode.postMessage({ type: 'prompt', prompt });
        
        input.value = '';
        input.style.height = 'auto';
        scrollToBottom();
    }
});

// Create Message Bubble
function createMessageBubble(role) {
    const div = document.createElement('div');
    div.className = `message ${role}-message`;
    div.innerHTML = `<div class="message-content"></div>`;
    return div;
}

function addMessage(role, text) {
    const bubble = createMessageBubble(role);
    bubble.querySelector('.message-content').innerHTML = parseMarkdown(text);
    messages.appendChild(bubble);
    scrollToBottom();
}

function scrollToBottom() {
    messages.scrollTop = messages.scrollHeight;
}

// Event Playback Engine
async function playbackEvents(events) {
    for (const event of events) {
        if (!isStreaming) break; // If cancelled
        
        // Small delay to simulate streaming
        await new Promise(r => setTimeout(r, 20));
        
        switch (event.event_type) {
            case 'reasoning_start':
            case 'reasoning_step':
                statusIndicator.className = 'status-indicator thinking';
                statusText.textContent = 'Pulse is reasoning...';
                break;
            case 'tool_start':
            case 'tool_progress':
                statusIndicator.className = 'status-indicator working';
                statusText.textContent = event.content || 'Pulse is working...';
                break;
            case 'llm_token':
                if (currentMessageContent) {
                    // event.content has the full accumulated text in streaming.py
                    // wait, in streaming.py event.content has the full response so far.
                    currentMessageContent.innerHTML = parseMarkdown(event.content);
                    scrollToBottom();
                }
                break;
            case 'task_progress':
                taskPanel.classList.remove('hidden');
                const progress = event.metadata?.progress || 0;
                taskProgressBar.style.width = `${progress}%`;
                taskProgressText.textContent = `${Math.round(progress)}%`;
                taskDetails.textContent = event.content;
                break;
            case 'tool_complete':
            case 'tool_failed':
            case 'verification_start':
            case 'verification_complete':
            case 'planning_start':
            case 'planning_step':
                // Just status updates
                statusText.textContent = event.content || 'Pulse is processing...';
                break;
        }
    }
    
    // Playback finished
    isStreaming = false;
    input.disabled = false;
    sendButton.disabled = false;
    statusIndicator.className = 'status-indicator idle';
    statusText.textContent = 'Pulse is Idle';
    setTimeout(() => {
        taskPanel.classList.add('hidden');
        taskProgressBar.style.width = '0%';
    }, 3000);
    input.focus();
}

// Listen for messages from Extension
window.addEventListener('message', async (event) => {
    const msg = event.data;
    
    if (msg.type === 'response_events') {
        await playbackEvents(msg.events);
    } else if (msg.type === 'error') {
        if (currentMessageContent && currentMessageContent.innerHTML === '') {
            currentMessageContent.innerHTML = `<span style="color: #ef4444;">Error: ${msg.content}</span>`;
        } else {
            addMessage('pulse', `*Error:* ${msg.content}`);
        }
        isStreaming = false;
        input.disabled = false;
        sendButton.disabled = false;
        statusIndicator.className = 'status-indicator idle';
        statusText.textContent = 'Pulse is Idle';
        input.focus();
    }
});
