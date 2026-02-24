import { useState, useEffect } from 'react'
import LoginPage from './components/LoginPage'
import ChatPage from './components/ChatPage'

const SESSION_KEY = 'negotiation_session'

export default function App() {
  const [session, setSession] = useState(null)

  const handleLogin = (sessionToken, builderName) => {
    const data = { session_token: sessionToken, builder_name: builderName }
    localStorage.setItem(SESSION_KEY, JSON.stringify(data))
    setSession(data)
  }

  const handleLogout = () => {
    localStorage.removeItem(SESSION_KEY)
    setSession(null)
  }

  return session ? (
    <ChatPage session={session} onLogout={handleLogout} />
  ) : (
    <LoginPage onLogin={handleLogin} />
  )
}
