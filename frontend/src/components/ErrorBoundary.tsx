import { Component, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export default class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback;
      return (
        <div className="flex items-center justify-center min-h-[70vh] pt-12">
          <div className="text-center p-8">
            <h3 className="text-[40px] font-semibold tracking-tight text-gray-900 dark:text-white mb-6">
              Something went wrong.
            </h3>
            <button
              onClick={() => window.location.reload()}
              className="inline-flex items-center px-6 py-2.5 bg-blue-600 text-white text-[15px] rounded-full hover:bg-blue-700 transition-colors"
            >
              Reload
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}