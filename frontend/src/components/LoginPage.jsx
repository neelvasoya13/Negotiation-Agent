import { useState } from 'react'

export default function LoginPage({ onLogin }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const res = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
      })
      const data = await res.json()
      if (!data.success) {
        setError(data.error || 'Login failed')
        return
      }
      onLogin(data.session_token, data.builder_name)
    } catch (err) {
      setError('Network error. Is the backend running on port 8000?')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <h1>Negotiation Chatbot</h1>
        <p className="subtitle">Construction materials price negotiation</p>
        <form onSubmit={handleSubmit}>
          <input
            type="email"
            placeholder="Email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            autoFocus
          />
          <input
            type="password"
            placeholder="Password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
          {error && <div className="error">{error}</div>}
          <button type="submit" disabled={loading}>
            {loading ? 'Signing in...' : 'Sign In'}
          </button>
        </form>
      </div>
      <style>{`
        .login-page {
          min-height: 100vh;
          display: flex;
          align-items: center;
          justify-content: center;
          padding: 1rem;
        }
        .login-card {
          background: var(--bg-card);
          border-radius: 16px;
          box-shadow: var(--shadow);
          padding: 2.5rem;
          width: 100%;
          max-width: 400px;
          border: 1px solid var(--border);
        }
        .login-card h1 {
          font-size: 1.75rem;
          margin-bottom: 0.25rem;
        }
        .login-card .subtitle {
          color: var(--text-secondary);
          font-size: 0.9rem;
          margin-bottom: 1.75rem;
        }
        .login-card input {
          display: block;
          width: 100%;
          padding: 0.875rem 1rem;
          margin-bottom: 1rem;
          background: var(--bg-input);
          border: 1px solid var(--border);
          border-radius: 10px;
          color: var(--text-primary);
          font-size: 1rem;
        }
        .login-card input::placeholder {
          color: var(--text-secondary);
        }
        .login-card input:focus {
          outline: none;
          border-color: var(--accent);
        }
        .login-card .error {
          color: #ef4444;
          font-size: 0.875rem;
          margin-bottom: 1rem;
          padding: 0.5rem;
          background: rgba(239, 68, 68, 0.1);
          border-radius: 8px;
        }
        .login-card button {
          width: 100%;
          padding: 0.875rem;
          background: var(--accent);
          color: white;
          border: none;
          border-radius: 10px;
          font-size: 1rem;
          font-weight: 600;
          cursor: pointer;
          transition: background 0.2s;
        }
        .login-card button:hover:not(:disabled) {
          background: var(--accent-hover);
        }
        .login-card button:disabled {
          opacity: 0.7;
          cursor: not-allowed;
        }
      `}</style>
    </div>
  )
}
