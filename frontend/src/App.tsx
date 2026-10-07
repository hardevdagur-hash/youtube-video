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
      <div className="flex items-center justify-center min-h-[200px]">
        <div className="animate-pulse text-sm text-gray-400">Loading...</div>
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
