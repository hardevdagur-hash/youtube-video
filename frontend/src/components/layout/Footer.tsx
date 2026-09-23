import { Link } from 'react-router-dom';
import { Github, FileText, Code2, Mail } from 'lucide-react';

const columns = [
  {
    title: 'Product',
    links: [
      { label: 'Metadata Export', to: '/metadata' },
      { label: 'Transcript Extractor', to: '/transcript' },
    ],
  },
  {
    title: 'Resources',
    links: [
      { label: 'Documentation', href: '#', icon: FileText },
      { label: 'YouTube API v3', href: 'https://developers.google.com/youtube/v3', icon: Code2, external: true },
      { label: 'GitHub', href: 'https://github.com', icon: Github, external: true },
    ],
  },
  {
    title: 'Company',
    links: [
      { label: 'Contact', href: '#', icon: Mail },
      { label: 'Version 2.0.0' },
    ],
  },
];

export default function Footer() {
  return (
    <footer className="border-t border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-gray-950">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-12 sm:py-16">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-8">
          <div className="col-span-2 md:col-span-1">
            <Link to="/" className="flex items-center gap-2 mb-3 no-underline">
              <img src="/static/logo.png" alt="Logo" className="h-7 w-auto rounded-md" />
              <span className="font-bold text-gray-900 dark:text-white text-sm">YouTube Export</span>
            </Link>
            <p className="text-sm text-gray-500 dark:text-gray-400 leading-relaxed max-w-xs">
              Export YouTube channel metadata and extract clean video transcripts with Whisper AI speech-to-text fallback. Built with the YouTube Data API v3.
            </p>
          </div>

          {columns.map((col) => (
            <div key={col.title}>
              <h4 className="text-xs font-semibold text-gray-900 dark:text-white uppercase tracking-wider mb-4">
                {col.title}
              </h4>
              <ul className="space-y-2.5">
                {col.links.map((link: any) => {
                  const Icon = link.icon;
                  const isExternal = link.external;
                  const Tag = link.to ? Link : link.href ? 'a' : 'span';
                  const props: any = link.to
                    ? { to: link.to }
                    : link.href
                      ? { href: link.href, ...(isExternal ? { target: '_blank', rel: 'noopener noreferrer' } : {}) }
                      : {};
                  return (
                    <li key={link.label}>
                      <Tag
                        {...props}
                        className="flex items-center gap-1.5 text-sm text-gray-500 dark:text-gray-400 hover:text-emerald-600 dark:hover:text-emerald-400 transition-colors no-underline"
                      >
                        {Icon && <Icon size={14} />}
                        {link.label}
                      </Tag>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </div>
      </div>
      <div className="border-t border-gray-100 dark:border-gray-800">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-5 flex flex-col sm:flex-row justify-between items-center gap-2">
          <p className="text-xs text-gray-400 dark:text-gray-500">
            &copy; 2026 YouTube Export Tool v2
          </p>
          <p className="text-xs text-gray-400 dark:text-gray-500">
            Powered by the YouTube Data API v3
          </p>
        </div>
      </div>
    </footer>
  );
}
