import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import './index.css'
import App from './App.tsx'
import { ArchitectureSvgPage } from './pages/architecture/ArchitectureSvgPage'
import { ArchitecturePage } from './pages/architecture/ArchitecturePage'
import { HomePage } from './pages/home/HomePage.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<HomePage />} />
        {/* The existing live dashboard, unchanged, now under /console — the
            marketing homepage owns "/" instead. */}
        <Route path="/architecture" element={<ArchitecturePage />} />
        {/* Vector variant, kept for print/export — see ArchitectureSvgPage. */}
        <Route path="/architecture/svg" element={<ArchitectureSvgPage />} />
        <Route path="/console" element={<App />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
)
