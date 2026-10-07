import { useState, useEffect } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { Github, LogOut, Moon, Sun, Menu, X } from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import { useTheme } from '../../theme/ThemeContext';
import { useAuth } from '../../auth/AuthContext';
import { Button } from '../ui';

const navLinks = [
  { label: 'Metadata', path: '/metadata' },
  { label: 'Transcript', path: '/transcript' },
  { label: 'Docs', path: '/docs' },
];

export default function Navbar() {
  const { dark, toggle } = useTheme();
  const { user, logout } = useAuth();
  const [scrolled, setScrolled] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const location = useLocation();
  const isHome = location.pathname === '/';

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 20);
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => window.removeEventListener('scroll', onScroll);
  }, []);

  useEffect(() => {
    setMenuOpen(false);
  }, [location]);

  const showBg = scrolled || !isHome;

  return (
    <header
      className={`fixed top-0 left-0 right-0 z-50 transition-all-300 ${
        showBg
          ? 'bg-white/95 dark:bg-gray-950/95 backdrop-blur-xl border-b border-gray-100 dark:border-gray-800 shadow-sm'
          : 'bg-transparent'
      }`}
    >
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex items-center justify-between h-16 lg:h-[68px]">
          {/* LEFT: MATRIX Brand + YouTube Export */}
          <Link to="/" className="flex items-center gap-3 no-underline group">
            <div className="flex items-center justify-center w-8 h-8 rounded-lg bg-gradient-to-br from-emerald-500 to-teal-700 text-white shadow-md shadow-emerald-500/20 group-hover:scale-105 transition-transform duration-200">
              <span className="font-black text-sm tracking-tighter">M</span>
            </div>
            <div className="flex items-center gap-2">
              <span className={`font-extrabold text-base tracking-wider uppercase ${showBg ? 'text-gray-900 dark:text-white' : 'text-white'}`}>
                MATRIX
              </span>
              <span className={`text-xs px-1.5 py-0.5 rounded font-mono font-medium ${showBg ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-400' : 'bg-white/20 text-white'}`}>
                v2
              </span>
              <span className={`hidden sm:inline-block w-px h-4 ${showBg ? 'bg-gray-300 dark:bg-gray-700' : 'bg-white/30'}`} />
              <span className={`hidden sm:inline-block text-sm font-semibold ${showBg ? 'text-gray-600 dark:text-gray-300' : 'text-white/90'}`}>
                YouTube Export
              </span>
            </div>
          </Link>

          {/* RIGHT: Navigation + Docs + Theme + GitHub */}
          <nav className="hidden md:flex items-center gap-1.5">
            {navLinks.map((link) => {
              const isActive = location.pathname === link.path;

              return (
                <Link
                  key={link.label}
                  to={link.path}
                  className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-all-200 no-underline ${
                    isActive
                      ? showBg
                        ? 'text-emerald-600 dark:text-emerald-400 bg-emerald-50 dark:bg-emerald-900/30'
                        : 'text-emerald-300 bg-white/15 shadow-sm'
                      : showBg
                        ? 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white hover:bg-gray-100 dark:hover:bg-gray-800'
                        : 'text-white/80 hover:text-white hover:bg-white/10'
                  }`}
                >
                  {link.label}
                </Link>
              );
            })}

            <div className={`h-4 w-px mx-1.5 ${showBg ? 'bg-gray-200 dark:bg-gray-800' : 'bg-white/20'}`} />

            <a
              href="https://github.com"
              target="_blank"
              rel="noopener noreferrer"
              className={`p-2 rounded-lg transition-all-200 ${
                showBg
                  ? 'text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white hover:bg-gray-100 dark:hover:bg-gray-800'
                  : 'text-white/80 hover:text-white hover:bg-white/10'
              }`}
              aria-label="GitHub Repository"
            >
              <Github size={18} />
            </a>

            <button
              onClick={toggle}
              className={`p-2 rounded-lg transition-all-200 ${
                showBg
                  ? 'text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white hover:bg-gray-100 dark:hover:bg-gray-800'
                  : 'text-white/80 hover:text-white hover:bg-white/10'
              }`}
              aria-label="Toggle dark mode"
            >
              {dark ? <Sun size={18} /> : <Moon size={18} />}
            </button>

            {user && (
              <button
                onClick={logout}
                className={`p-2 rounded-lg transition-all-200 ${
                  showBg
                    ? 'text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white hover:bg-gray-100 dark:hover:bg-gray-800'
                    : 'text-white/80 hover:text-white hover:bg-white/10'
                }`}
                aria-label={`Sign out ${user.username}`}
                title={`Sign out ${user.username}`}
              >
                <LogOut size={18} />
              </button>
            )}
          </nav>

          <button
            onClick={() => setMenuOpen(!menuOpen)}
            className={`md:hidden p-2 rounded-lg transition-all-200 ${
              showBg ? 'text-gray-600 dark:text-gray-300' : 'text-white'
            }`}
            aria-label="Toggle menu"
          >
            {menuOpen ? <X size={22} /> : <Menu size={22} />}
          </button>
        </div>
      </div>

      <AnimatePresence>
        {menuOpen && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className="md:hidden border-t border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-950 shadow-lg overflow-hidden"
          >
            <div className="px-4 py-4 space-y-1">
              {navLinks.map((link) => {
                const isActive = location.pathname === link.path;

                return (
                  <Link
                    key={link.label}
                    to={link.path}
                    className={`block px-3 py-2.5 rounded-lg text-sm font-medium no-underline ${
                      isActive
                        ? 'text-emerald-600 dark:text-emerald-400 bg-emerald-50 dark:bg-emerald-900/30'
                        : 'text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800'
                    }`}
                  >
                    {link.label}
                  </Link>
                );
              })}
              <a
                href="https://github.com"
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-2 px-3 py-2.5 rounded-lg text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 no-underline"
              >
                <Github size={16} /> GitHub
              </a>
              <button
                onClick={toggle}
                className="flex items-center gap-2 px-3 py-2.5 rounded-lg text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 w-full"
              >
                {dark ? <Sun size={16} /> : <Moon size={16} />} {dark ? 'Light Mode' : 'Dark Mode'}
              </button>
              {user && (
                <button
                  onClick={logout}
                  className="flex items-center gap-2 px-3 py-2.5 rounded-lg text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 w-full"
                >
                  <LogOut size={16} /> Sign out ({user.username})
                </button>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </header>
  );
}
