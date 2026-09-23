import { motion } from 'framer-motion';
import { ExternalLink, Code2, Terminal, BookOpen, Check, Layers, Cpu, ShieldCheck } from 'lucide-react';
import { Container, Card, Badge, Button } from '../components/ui';

const endpoints = [
  {
    method: 'GET',
    path: '/api/health',
    description: 'Service health check, database status, redis connectivity, and active export counter.',
    category: 'System',
  },
  {
    method: 'POST',
    path: '/api/export',
    description: 'Start asynchronous metadata extraction for a channel. Returns an immediate job_id.',
    category: 'Metadata',
  },
  {
    method: 'GET',
    path: '/api/export/{job_id}/progress',
    description: 'Poll real-time extraction progress, item counts, and status for an ongoing export job.',
    category: 'Metadata',
  },
  {
    method: 'GET',
    path: '/api/export/{job_id}/download',
    description: 'Stream and download completed CSV export file.',
    category: 'Metadata',
  },
  {
    method: 'GET',
    path: '/api/transcript/{video_id}',
    description: 'Retrieve video transcripts with automatic fallback to Whisper AI speech-to-text.',
    category: 'Transcripts',
  },
  {
    method: 'GET',
    path: '/api/metrics',
    description: 'Prometheus metrics endpoint for system observability, throughput, and error rates.',
    category: 'Monitoring',
  },
];

export default function Docs() {
  return (
    <div className="pt-24 pb-20">
      <Container>
        {/* Header */}
        <div className="mb-10 text-center max-w-2xl mx-auto">
          <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-emerald-50 dark:bg-emerald-950/60 border border-emerald-200 dark:border-emerald-800 text-emerald-700 dark:text-emerald-300 text-xs font-semibold uppercase tracking-wider mb-4">
            <BookOpen size={13} />
            API & Architecture Documentation
          </div>
          <h1 className="text-3xl sm:text-4xl font-extrabold text-gray-900 dark:text-white tracking-tight mb-3">
            MATRIX YouTube Platform API
          </h1>
          <p className="text-base text-gray-600 dark:text-gray-400">
            Comprehensive reference for backend endpoints, async processing jobs, channel metadata exports, and transcript extraction.
          </p>
          <div className="mt-6 flex justify-center gap-3">
            <a
              href="http://localhost:8000/docs"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white text-sm font-semibold shadow-md shadow-emerald-600/20 transition-colors no-underline"
            >
              <span>Interactive Swagger UI</span>
              <ExternalLink size={14} />
            </a>
            <a
              href="http://localhost:8000/redoc"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 text-gray-800 dark:text-gray-200 text-sm font-semibold transition-colors no-underline"
            >
              <span>ReDoc Reference</span>
              <ExternalLink size={14} />
            </a>
          </div>
        </div>

        {/* Quick Architecture Grid */}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-5 mb-12">
          <Card className="p-5 border border-gray-200/80 dark:border-gray-800">
            <div className="flex items-center gap-3 mb-2">
              <div className="w-9 h-9 rounded-lg bg-emerald-50 dark:bg-emerald-950/50 flex items-center justify-center text-emerald-600 dark:text-emerald-400">
                <Cpu size={18} />
              </div>
              <h4 className="font-bold text-gray-900 dark:text-white text-sm">Async Job Pipeline</h4>
            </div>
            <p className="text-xs text-gray-500 dark:text-gray-400 leading-relaxed">
              Long-running metadata exports execute asynchronously via non-blocking background workers with real-time polling.
            </p>
          </Card>

          <Card className="p-5 border border-gray-200/80 dark:border-gray-800">
            <div className="flex items-center gap-3 mb-2">
              <div className="w-9 h-9 rounded-lg bg-teal-50 dark:bg-teal-950/50 flex items-center justify-center text-teal-600 dark:text-teal-400">
                <Layers size={18} />
              </div>
              <h4 className="font-bold text-gray-900 dark:text-white text-sm">Multi-Provider Fallback</h4>
            </div>
            <p className="text-xs text-gray-500 dark:text-gray-400 leading-relaxed">
              Transcript service checks manual captions, then auto-captions, and falls back to local/API Whisper speech-to-text models.
            </p>
          </Card>

          <Card className="p-5 border border-gray-200/80 dark:border-gray-800">
            <div className="flex items-center gap-3 mb-2">
              <div className="w-9 h-9 rounded-lg bg-violet-50 dark:bg-violet-950/50 flex items-center justify-center text-violet-600 dark:text-violet-400">
                <ShieldCheck size={18} />
              </div>
              <h4 className="font-bold text-gray-900 dark:text-white text-sm">Enterprise Observability</h4>
            </div>
            <p className="text-xs text-gray-500 dark:text-gray-400 leading-relaxed">
              End-to-end request tracing, correlation IDs, OpenTelemetry integration, and Prometheus telemetry metrics at <code>/api/metrics</code>.
            </p>
          </Card>
        </div>

        {/* REST API Endpoints Table */}
        <div className="bg-white dark:bg-gray-900 rounded-2xl border border-gray-200/80 dark:border-gray-800 shadow-sm overflow-hidden mb-12">
          <div className="p-6 border-b border-gray-100 dark:border-gray-800 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Code2 size={20} className="text-emerald-600 dark:text-emerald-400" />
              <h2 className="text-lg font-bold text-gray-900 dark:text-white">
                Core REST API Endpoints
              </h2>
            </div>
            <span className="text-xs font-mono text-gray-400">
              Base: http://localhost:8000
            </span>
          </div>

          <div className="divide-y divide-gray-100 dark:divide-gray-800/80 overflow-x-auto">
            {endpoints.map((ep) => (
              <div key={ep.path} className="p-4 sm:p-5 flex flex-col sm:flex-row sm:items-center justify-between gap-3 hover:bg-gray-50/70 dark:hover:bg-gray-800/40 transition-colors">
                <div className="flex items-start sm:items-center gap-3">
                  <span
                    className={`text-xs font-mono font-bold px-2.5 py-1 rounded-md ${
                      ep.method === 'GET'
                        ? 'bg-blue-50 text-blue-700 dark:bg-blue-950/70 dark:text-blue-300 border border-blue-200 dark:border-blue-800'
                        : 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950/70 dark:text-emerald-300 border border-emerald-200 dark:border-emerald-800'
                    }`}
                  >
                    {ep.method}
                  </span>
                  <div>
                    <code className="text-sm font-semibold text-gray-900 dark:text-white">
                      {ep.path}
                    </code>
                    <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                      {ep.description}
                    </p>
                  </div>
                </div>
                <div className="flex items-center gap-2 self-start sm:self-center">
                  <span className="text-[11px] font-medium px-2 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300">
                    {ep.category}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Quick Example cURL */}
        <div className="bg-gray-900 dark:bg-black rounded-2xl p-6 border border-gray-800 shadow-lg text-white">
          <div className="flex items-center gap-2 mb-3 text-emerald-400 text-xs font-mono">
            <Terminal size={15} />
            <span>QUICKSTART CURL REQUEST</span>
          </div>
          <pre className="text-xs font-mono text-gray-300 overflow-x-auto p-4 rounded-xl bg-gray-950 border border-gray-800/80">
{`# 1. Check system health
curl http://localhost:8000/api/health

# 2. Extract transcript for a video
curl http://localhost:8000/api/transcript/dQw4w9WgXcQ

# 3. Start a metadata export
curl -X POST http://localhost:8000/api/export \\
  -H "Content-Type: application/json" \\
  -d '{"channel_input": "@GoogleDevelopers", "max_videos": 50}'`}
          </pre>
        </div>
      </Container>
    </div>
  );
}
