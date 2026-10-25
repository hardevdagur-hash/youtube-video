import { useState, useEffect } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { LogOut, Moon, Sun, Menu, X } from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import { useTheme } from '../../theme/ThemeContext';
import { useAuth } from '../../auth/AuthContext';

const navLinks = [
  { label: 'Transcript', path: '/transcript' },
];

export default function Navbar() {
  const { dark, toggle } = useTheme();
  const { user, logout } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);
  const location = useLocation();

  useEffect(() => {
    setMenuOpen(false);
  }, [location]);

  const iconBtn =
    'p-1.5 rounded-full text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white transition-colors-200';

  return (
    <header className="fixed top-0 left-0 right-0 z-50 glass border-b border-black/5 dark:border-white/10">
      <div className="max-w-5xl mx-auto px-4 sm:px-6">
        <div className="flex items-center justify-between h-12">
          <Link to="/transcript" className="flex items-center no-underline" aria-label="Matrix home">
            <img src="/static/logo.png" alt="Matrix" className="h-5 w-auto dark:hidden" />
            <img src="/static/logo-white.png" alt="Matrix" className="h-5 w-auto hidden dark:block" />
          </Link>

          <nav className="hidden md:flex items-center gap-5">
            {navLinks.map((link) => {
              const isActive = location.pathname === link.path;
              return (
                <Link
                  key={link.label}
                  to={link.path}
                  className={`text-xs no-underline transition-colors-200 ${
                    isActive
                      ? 'text-gray-900 dark:text-white'
                      : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white'
                  }`}
                >
                  {link.label}
                </Link>
              );
            })}

            <button onClick={toggle} className={iconBtn} aria-label="Toggle dark mode">
              {dark ? <Sun size={15} /> : <Moon size={15} />}
            </button>

            {user && (
              <button
                onClick={logout}
                className={iconBtn}
                aria-label={`Sign out ${user.username}`}
                title={`Sign out ${user.username}`}
              >
                <LogOut size={15} />
              </button>
            )}
          </nav>

          <button
            onClick={() => setMenuOpen(!menuOpen)}
            className={`md:hidden ${iconBtn}`}
            aria-label="Toggle menu"
          >
            {menuOpen ? <X size={18} /> : <Menu size={18} />}
          </button>
        </div>
      </div>

      <AnimatePresence>
        {menuOpen && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className="md:hidden overflow-hidden"
          >
            <div className="px-6 pb-6 pt-2 space-y-1">
              {navLinks.map((link) => (
                <Link
                  key={link.label}
                  to={link.path}
                  className="block py-2 text-2xl font-semibold text-gray-900 dark:text-white no-underline"
                >
                  {link.label}
                </Link>
              ))}
              <button
                onClick={toggle}
                className="flex items-center gap-2 py-2 text-sm text-gray-600 dark:text-gray-400 w-full"
              >
                {dark ? <Sun size={15} /> : <Moon size={15} />} {dark ? 'Light' : 'Dark'}
              </button>
              {user && (
                <button
                  onClick={logout}
                  className="flex items-center gap-2 py-2 text-sm text-gray-600 dark:text-gray-400 w-full"
                >
                  <LogOut size={15} /> Sign out
                </button>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </header>
  );
}
