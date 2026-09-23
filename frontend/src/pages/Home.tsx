import { motion } from 'framer-motion';
import { Download, MessageSquareText, Sparkles, ShieldCheck, Zap, Database } from 'lucide-react';
import WorkflowCard from '../components/home/WorkflowCard';

const workflows = [
  {
    icon: Download,
    badge: 'Metadata & CSV',
    title: 'Download Metadata',
    description:
      'Export complete channel metadata, video listings, view statistics, and engagement metrics directly to formatted CSV.',
    features: [
      'Channel handle & custom URL lookup',
      'Exhaustive uploaded video discovery',
      'Metrics: views, likes, durations',
      'Streaming high-capacity CSV export',
    ],
    cta: 'Open Metadata Downloader',
    to: '/metadata',
    accentColor: 'emerald' as const,
  },
  {
    icon: MessageSquareText,
    badge: 'Speech-to-Text',
    title: 'URL → Transcript',
    description:
      'Extract high-accuracy transcripts from any YouTube video with automatic fallback to Whisper AI speech-to-text.',
    features: [
      'Official manual & auto-sync captions',
      'Whisper AI audio STT fallback',
      'Timestamped & clean text formats',
      'Multi-language translation support',
    ],
    cta: 'Extract Transcript',
    to: '/transcript',
    accentColor: 'teal' as const,
  },
];

const highlights = [
  { icon: Zap, label: 'Fast Async Processing' },
  { icon: ShieldCheck, label: 'Enterprise Reliability' },
  { icon: Database, label: 'YouTube Data API v3' },
  { icon: Sparkles, label: 'Whisper AI Speech-to-Text' },
];

export default function Home() {
  return (
    <>
      {/* Hero Section */}
      <section className="relative overflow-hidden bg-gradient-to-br from-emerald-700 via-emerald-600 to-teal-800 dark:from-gray-950 dark:via-emerald-950/40 dark:to-gray-900 pt-24 lg:pt-28 pb-16 lg:pb-20">
        {/* Subtle geometric & radial background effects */}
        <div className="absolute inset-0 pointer-events-none">
          <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_top,rgba(255,255,255,0.12),transparent_70%)]" />
          <div className="absolute top-1/4 -left-20 w-96 h-96 bg-emerald-400/10 rounded-full blur-3xl" />
          <div className="absolute bottom-0 right-1/4 w-96 h-96 bg-teal-400/10 rounded-full blur-3xl" />
        </div>

        <div className="relative max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
            className="text-center max-w-3xl mx-auto"
          >
            {/* Pill Badge */}
            <div className="inline-flex items-center gap-2 px-3.5 py-1.5 rounded-full bg-white/10 dark:bg-white/5 border border-white/20 text-white/95 text-xs font-semibold tracking-wide uppercase mb-6 shadow-sm">
              <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
              MATRIX YouTube Platform v2
            </div>

            {/* Headline */}
            <h1 className="text-3xl sm:text-5xl lg:text-6xl font-extrabold leading-[1.12] tracking-tight text-white mb-5">
              Export YouTube Data & Transcribe Audio
            </h1>

            {/* Supporting Text */}
            <p className="text-base sm:text-lg leading-relaxed text-emerald-100/90 dark:text-gray-300 max-w-2xl mx-auto mb-8">
              Choose your workflow to get started. Extract complete channel catalogs to formatted CSV or transcribe video audio with official captions and Whisper AI speech-to-text fallback.
            </p>

            {/* Trust / Capability Badges */}
            <div className="flex flex-wrap items-center justify-center gap-3 sm:gap-6 text-xs text-white/80 dark:text-gray-400 pt-2">
              {highlights.map((item) => {
                const Icon = item.icon;
                return (
                  <div key={item.label} className="flex items-center gap-1.5 px-3 py-1 rounded-md bg-white/5 border border-white/10 backdrop-blur-sm">
                    <Icon size={14} className="text-emerald-300 dark:text-emerald-400" />
                    <span>{item.label}</span>
                  </div>
                );
              })}
            </div>
          </motion.div>
        </div>
      </section>

      {/* Feature Cards Section — BALANCED 2-COLUMN ROW ON DESKTOP */}
      <section className="relative z-10 -mt-8 pb-20 sm:pb-24">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="grid grid-cols-1 md:grid-cols-2 max-w-4xl mx-auto gap-6 lg:gap-8 items-stretch">
            {workflows.map((wf, i) => (
              <motion.div
                key={wf.title}
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.5, delay: i * 0.1, ease: [0.16, 1, 0.3, 1] }}
                className="h-full"
              >
                <WorkflowCard {...wf} />
              </motion.div>
            ))}
          </div>
        </div>
      </section>
    </>
  );
}
