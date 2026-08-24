import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import './index.css'
import App from './App.tsx'
import { HomePage } from './pages/home/HomePage.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<HomePage />} />
        {/* The existing live dashboard, unchanged, now under /console — the
            marketing homepage owns "/" instead. */}
        <Route path="/console" element={<App />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
)
