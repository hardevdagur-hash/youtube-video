import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { installAuthInterceptor } from './auth/AuthContext';
import './index.css';

installAuthInterceptor();

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
