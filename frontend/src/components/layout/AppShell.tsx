import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { navigation } from './navigation'
import { HealthStatus } from './HealthStatus'
import { Button, VisuallyHidden } from '../ui'

function useTheme() {
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    const saved = localStorage.getItem('cf-theme')
    if (saved === 'light' || saved === 'dark') return saved
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  })
  const toggle = () => setTheme((current) => {
    const next = current === 'light' ? 'dark' : 'light'
    localStorage.setItem('cf-theme', next)
    return next
  })
  return { theme, toggle }
}

export function AppShell() {
  const [menuOpen, setMenuOpen] = useState(false)
  const [isMobile, setIsMobile] = useState(() => window.matchMedia('(max-width: 760px)').matches)
  const menuButtonRef = useRef<HTMLButtonElement>(null)
  const sidebarRef = useRef<HTMLElement>(null)
  const { theme, toggle } = useTheme()
  useEffect(() => { document.documentElement.dataset.theme = theme }, [theme])
  useEffect(() => {
    const media = window.matchMedia('(max-width: 760px)')
    const update = () => setIsMobile(media.matches)
    update()
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [])
  useEffect(() => {
    if (menuOpen) sidebarRef.current?.querySelector<HTMLElement>('a[href]')?.focus()
  }, [menuOpen])

  const closeMenu = () => {
    setMenuOpen(false)
    menuButtonRef.current?.focus()
  }

  const handleMenuKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      closeMenu()
      return
    }
    if (event.key !== 'Tab' || !menuOpen) return
    const focusable = sidebarRef.current?.querySelectorAll<HTMLElement>('a[href], button:not(:disabled), [tabindex]:not([tabindex="-1"])')
    if (!focusable?.length) return
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last?.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first?.focus()
    }
  }

  return <div className="app-shell">
    <a className="skip-link" href="#main-content" onClick={(event) => { event.preventDefault(); document.getElementById('main-content')?.focus() }}>Aller au contenu</a>
    <aside ref={sidebarRef} className={`sidebar ${menuOpen ? 'sidebar--open' : ''}`} aria-label="Navigation principale" aria-hidden={isMobile && !menuOpen ? true : undefined} inert={isMobile && !menuOpen}>
      <div className="brand"><span className="brand-mark" aria-hidden="true">c.</span><span>comptes<span className="brand-light">familiaux</span></span></div>
      <p className="nav-caption">ESPACE FAMILIAL</p>
      <nav id="main-navigation" aria-label="Pages">
        {navigation.map((item) => <NavLink key={item.path} to={item.path} end={item.path === '/'} onClick={() => menuOpen && closeMenu()} onKeyDown={handleMenuKeyDown} className={({ isActive }) => `nav-link ${isActive ? 'is-active' : ''}`}>
          <span className="nav-icon" aria-hidden="true">{item.icon}</span><span>{item.label}</span>
        </NavLink>)}
      </nav>
      <div className="sidebar-bottom"><HealthStatus /><p>Données conservées sur cet appareil</p></div>
    </aside>
    {menuOpen && <button className="mobile-scrim" aria-label="Fermer le menu" onClick={closeMenu} />}
    <div className="workspace">
      <header className="topbar">
        <Button ref={menuButtonRef} className="menu-toggle" aria-expanded={menuOpen} aria-controls="main-navigation" aria-label={menuOpen ? 'Fermer le menu' : 'Ouvrir le menu'} onClick={() => menuOpen ? closeMenu() : setMenuOpen(true)}>
          <span aria-hidden="true">☰</span><VisuallyHidden>{menuOpen ? 'Fermer le menu' : 'Ouvrir le menu'}</VisuallyHidden>
        </Button>
        <div className="breadcrumb"><span>Comptes familiaux</span><span aria-hidden="true">/</span><span className="breadcrumb-current">Espace privé</span></div>
        <div className="topbar-actions"><span className="local-pill"><span aria-hidden="true">●</span> Local</span><div className="mobile-health"><HealthStatus compact /></div><Button aria-label={`Activer le thème ${theme === 'light' ? 'sombre' : 'clair'}`} onClick={toggle} className="theme-toggle"><span aria-hidden="true">{theme === 'light' ? '☾' : '☀'}</span></Button></div>
      </header>
      <main id="main-content" className="main-content" tabIndex={-1}><Outlet /></main>
      <footer className="app-footer"><span>Comptes familiaux</span><span>Privé par conception <span aria-hidden="true">·</span> aucune donnée ne quitte cet appareil</span></footer>
    </div>
  </div>
}
