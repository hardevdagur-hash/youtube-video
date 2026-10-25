import { Link } from 'react-router-dom';

export default function NotFound() {
  return (
    <div className="flex items-center justify-center min-h-[70vh] pt-12">
      <div className="text-center p-8">
        <h1 className="text-[48px] font-semibold tracking-tight text-gray-900 dark:text-white mb-6">
          Page not found.
        </h1>
        <Link
          to="/"
          className="inline-flex items-center px-6 py-2.5 bg-blue-600 text-white text-[15px] rounded-full hover:bg-blue-700 transition-colors no-underline"
        >
          Go home
        </Link>
      </div>
    </div>
  );
}
