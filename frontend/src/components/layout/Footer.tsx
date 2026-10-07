import { Link } from 'react-router-dom';

export default function Footer() {
  return (
    <footer className="border-t border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-gray-950">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
        <div>
          <Link to="/transcript" className="flex items-center gap-2 mb-2 no-underline">
            <img src="/static/logo.png" alt="Logo" className="h-7 w-auto rounded-md" />
            <span className="font-bold text-gray-900 dark:text-white text-sm">YouTube Transcripts</span>
          </Link>
          <p className="text-sm text-gray-500 dark:text-gray-400 leading-relaxed max-w-md">
            Transcripts for single videos and whole channels: original captions first, speech-to-text
            fallback, optional Simple English or Simple Hindi rewriting, and CSV export.
          </p>
        </div>
        <p className="text-xs text-gray-400 dark:text-gray-500">Uses the YouTube Data API v3</p>
      </div>
    </footer>
  );
}
