import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { Suspense, lazy } from 'react';
import { ThemeProvider } from './theme/ThemeContext';
import ErrorBoundary from './components/ErrorBoundary';
import Layout from './components/layout/Layout';
import { AuthProvider, RequireAuth } from './auth/AuthContext';
import Home from './pages/Home';
import NotFound from './pages/NotFound';

const Metadata = lazy(() => import('./pages/Metadata'));
const Transcript = lazy(() => import('./pages/Transcript'));
const Docs = lazy(() => import('./pages/Docs'));

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
                <Route path="/" element={<Home />} />
                <Route path="/metadata" element={<RequireAuth><SuspenseWrapper><Metadata /></SuspenseWrapper></RequireAuth>} />
                <Route path="/transcript" element={<RequireAuth><SuspenseWrapper><Transcript /></SuspenseWrapper></RequireAuth>} />
                <Route path="/docs" element={<SuspenseWrapper><Docs /></SuspenseWrapper>} />
                <Route path="*" element={<NotFound />} />
              </Route>
            </Routes>
          </ErrorBoundary>
        </AuthProvider>
      </ThemeProvider>
    </BrowserRouter>
  );
}
