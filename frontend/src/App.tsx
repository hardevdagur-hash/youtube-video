import { BrowserRouter, Navigate, Routes, Route } from 'react-router-dom';
import { Suspense, lazy } from 'react';
import { ThemeProvider } from './theme/ThemeContext';
import ErrorBoundary from './components/ErrorBoundary';
import Layout from './components/layout/Layout';
import { AuthProvider, RequireAuth } from './auth/AuthContext';
import NotFound from './pages/NotFound';

const Transcript = lazy(() => import('./pages/Transcript'));

function SuspenseWrapper({ children }: { children: React.ReactNode }) {
  return (
    <Suspense fallback={
      <div className="flex items-center justify-center min-h-[70vh]">
        <div className="w-5 h-5 rounded-full border-2 border-gray-300 border-t-gray-600 animate-spin" aria-label="Loading" />
      </div>
    }>
      {children}
    </Suspense>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <ThemeProvider>
        <AuthProvider>
          <ErrorBoundary>
            <Routes>
              <Route element={<Layout />}>
                <Route path="/" element={<Navigate to="/transcript" replace />} />
                <Route path="/transcript" element={<RequireAuth><SuspenseWrapper><Transcript /></SuspenseWrapper></RequireAuth>} />
                <Route path="*" element={<NotFound />} />
              </Route>
            </Routes>
          </ErrorBoundary>
        </AuthProvider>
      </ThemeProvider>
    </BrowserRouter>
  );
}
