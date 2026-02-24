import { useState, useRef, useEffect } from 'react'

const API_BASE = '/api'

export default function ChatPage({ session, onLogout }) {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(true)
  const [conversationEnded, setConversationEnded] = useState(false)
  const [error, setError] = useState('')
  const [chatReady, setChatReady] = useState(false)
  const messagesEndRef = useRef(null)

  useEffect(() => {
    let cancelled = false
    async function init() {
      try {
        const res = await fetch(`${API_BASE}/chat/start`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_token: session.session_token }),
        })
        const data = await res.json()
        if (!cancelled) {
          setMessages(data.chat || [])
          setConversationEnded(data.conversation_ended || false)
          setChatReady(true)
        }
      } catch (err) {
        if (!cancelled) {
          setError('Failed to initialize chat')
          setChatReady(true)
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    init()
    return () => { cancelled = true }
  }, [session.session_token])

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }

  useEffect(() => {
    scrollToBottom()
  }, [messages, loading])

  const sendMessage = async (text) => {
    if (!text.trim() || loading || conversationEnded || !chatReady) return
    const userMsg = text.trim()
    setInput('')
    setMessages((prev) => [...prev, { role: 'user', content: userMsg }])
    setLoading(true)
    setError('')

    try {
      const res = await fetch(`${API_BASE}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: userMsg,
          session_token: session.session_token,
        }),
      })
      const data = await res.json()
      if (data.error) {
        setError(data.error)
        setMessages((prev) => prev.slice(0, -1))
        return
      }
      const chat = data.chat || []
      if (chat.length > 0) {
        setMessages(chat)
      }
      setConversationEnded(data.conversation_ended || false)
    } catch (err) {
      setError('Failed to send message. Is the backend running?')
      setMessages((prev) => prev.slice(0, -1))
    } finally {
      setLoading(false)
    }
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage(input)
    }
  }

  const startNewChat = async () => {
    setLoading(true)
    setError('')
    try {
      await fetch(`${API_BASE}/chat/start-new`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_token: session.session_token }),
      })
      setMessages([])
      setConversationEnded(false)
      setChatReady(false)
      const res = await fetch(`${API_BASE}/chat/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_token: session.session_token }),
      })
      const data = await res.json()
      setMessages(data.chat || [])
      setConversationEnded(data.conversation_ended || false)
      setChatReady(true)
    } catch (err) {
      setError('Failed to reset chat')
      setChatReady(true)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="chat-page">
      <header>
        <h2>Negotiation Chatbot</h2>
        <div className="header-actions">
          <span className="builder-name">{session.builder_name}</span>
          {conversationEnded && (
            <button className="btn-new" onClick={startNewChat} disabled={loading}>
              Start New Chat
            </button>
          )}
          <button className="btn-logout" onClick={onLogout}>
            Logout
          </button>
        </div>
      </header>
      <div className="chat-container">
        <div className="messages">
          {messages.length === 0 && !loading && (
            <div className="empty-state">
              <p>Ask about construction materials, quantities, and pricing.</p>
              <p className="hint">e.g. &quot;What is your rate for 500 bags of ACC cement?&quot;</p>
            </div>
          )}
          {messages.map((msg, i) => (
            <div key={i} className={`bubble ${msg.role}`}>
              <div className="bubble-inner">{msg.content}</div>
            </div>
          ))}
          {loading && (
            <div className="bubble assistant typing">
              <div className="typing-dots">
                <span></span><span></span><span></span>
              </div>
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>
        {error && <div className="error-bar">{error}</div>}
        <div className="input-area">
          <input
            type="text"
            placeholder={
              !chatReady
                ? 'Initializing...'
                : conversationEnded
                  ? 'Conversation ended'
                  : 'Type your message... (Enter to send)'
            }
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={loading || conversationEnded || !chatReady}
          />
          <button
            onClick={() => sendMessage(input)}
            disabled={loading || conversationEnded || !chatReady || !input.trim()}
          >
            {loading ? (
              <span className="spinner"></span>
            ) : (
              'Send'
            )}
          </button>
        </div>
      </div>
      <style>{`
        .chat-page {
          display: flex;
          flex-direction: column;
          height: 100vh;
          background: var(--bg-dark);
        }
        .chat-page header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 1rem 1.5rem;
          background: var(--bg-card);
          border-bottom: 1px solid var(--border);
          box-shadow: var(--shadow);
        }
        .chat-page header h2 {
          font-size: 1.25rem;
        }
        .header-actions {
          display: flex;
          align-items: center;
          gap: 1rem;
        }
        .builder-name {
          color: var(--text-secondary);
          font-size: 0.9rem;
        }
        .btn-new, .btn-logout {
          padding: 0.5rem 1rem;
          border-radius: 8px;
          font-size: 0.875rem;
          cursor: pointer;
          transition: all 0.2s;
        }
        .btn-new {
          background: var(--accent);
          color: white;
          border: none;
        }
        .btn-new:hover:not(:disabled) {
          background: var(--accent-hover);
        }
        .btn-new:disabled { opacity: 0.7; cursor: not-allowed; }
        .btn-logout {
          background: transparent;
          color: var(--text-secondary);
          border: 1px solid var(--border);
        }
        .btn-logout:hover { color: var(--text-primary); border-color: var(--text-secondary); }
        .chat-container {
          flex: 1;
          display: flex;
          flex-direction: column;
          max-width: 800px;
          width: 100%;
          margin: 0 auto;
          padding: 1rem;
          overflow: hidden;
        }
        .messages {
          flex: 1;
          overflow-y: auto;
          padding: 1rem 0;
          display: flex;
          flex-direction: column;
          gap: 1rem;
        }
        .empty-state {
          color: var(--text-secondary);
          text-align: center;
          padding: 2rem;
        }
        .empty-state .hint { font-size: 0.9rem; margin-top: 0.5rem; opacity: 0.8; }
        .bubble {
          max-width: 85%;
          align-self: flex-start;
          animation: fadeIn 0.3s ease;
        }
        .bubble.user {
          align-self: flex-end;
        }
        .bubble-inner {
          padding: 0.875rem 1.25rem;
          border-radius: 18px;
          line-height: 1.5;
          box-shadow: var(--shadow);
        }
        .bubble.user .bubble-inner {
          background: var(--user-bubble);
          color: white;
          border-bottom-right-radius: 4px;
        }
        .bubble.assistant .bubble-inner {
          background: var(--ai-bubble);
          border: 1px solid var(--border);
          border-bottom-left-radius: 4px;
        }
        .bubble.typing .bubble-inner { padding: 1rem 1.5rem; }
        .typing-dots {
          display: flex;
          gap: 6px;
        }
        .typing-dots span {
          width: 8px;
          height: 8px;
          background: var(--text-secondary);
          border-radius: 50%;
          animation: bounce 1.4s ease-in-out infinite;
        }
        .typing-dots span:nth-child(1) { animation-delay: 0s; }
        .typing-dots span:nth-child(2) { animation-delay: 0.2s; }
        .typing-dots span:nth-child(3) { animation-delay: 0.4s; }
        @keyframes bounce {
          0%, 80%, 100% { transform: scale(0.8); opacity: 0.5; }
          40% { transform: scale(1); opacity: 1; }
        }
        @keyframes fadeIn {
          from { opacity: 0; transform: translateY(8px); }
          to { opacity: 1; transform: translateY(0); }
        }
        .error-bar {
          background: rgba(239, 68, 68, 0.15);
          color: #ef4444;
          padding: 0.5rem 1rem;
          border-radius: 8px;
          font-size: 0.875rem;
          margin-bottom: 0.5rem;
        }
        .input-area {
          display: flex;
          gap: 0.5rem;
          padding: 0.5rem 0;
        }
        .input-area input {
          flex: 1;
          padding: 0.875rem 1.25rem;
          background: var(--bg-input);
          border: 1px solid var(--border);
          border-radius: 12px;
          color: var(--text-primary);
          font-size: 1rem;
        }
        .input-area input:focus {
          outline: none;
          border-color: var(--accent);
        }
        .input-area input:disabled {
          opacity: 0.6;
          cursor: not-allowed;
        }
        .input-area button {
          padding: 0.875rem 1.5rem;
          background: var(--accent);
          color: white;
          border: none;
          border-radius: 12px;
          font-weight: 600;
          cursor: pointer;
          min-width: 80px;
          display: flex;
          align-items: center;
          justify-content: center;
          transition: background 0.2s;
        }
        .input-area button:hover:not(:disabled) { background: var(--accent-hover); }
        .input-area button:disabled { opacity: 0.6; cursor: not-allowed; }
        .spinner {
          width: 20px;
          height: 20px;
          border: 2px solid rgba(255,255,255,0.3);
          border-top-color: white;
          border-radius: 50%;
          animation: spin 0.8s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
      `}</style>
    </div>
  )
}
