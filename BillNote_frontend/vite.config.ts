import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import fs from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'
import tailwindcss from '@tailwindcss/vite'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

function readAppVersion() {
  const fallbackVersion = '0.0.0'

  try {
    const tauriConfigPath = path.resolve(__dirname, 'src-tauri/tauri.conf.json')
    const tauriConfig = JSON.parse(fs.readFileSync(tauriConfigPath, 'utf-8')) as { version?: string }
    return tauriConfig.version || fallbackVersion
  }
  catch {
    return fallbackVersion
  }
}

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  // 在 Docker 环境中，父目录可能没有 .env 文件，使用当前目录
  const envDir = process.env.DOCKER_BUILD ? __dirname : path.resolve(__dirname, '../')
  const env = loadEnv(mode, envDir)

  /**
   * Asset base. The two consumers disagree, so this cannot be a constant:
   *
   * - Web (nginx / docker-compose): index.html is served for EVERY route, so a
   *   relative './assets/x.js' resolves against the current path. A deep link
   *   like /settings/general asks for /settings/assets/x.js, gets index.html
   *   back with 200, and the app dies on a module parse error — a blank page on
   *   reload, for every route except '/'. Absolute '/' is the only correct base.
   * - Tauri (pnpm build --mode tauri): the bundle is loaded from the app's own
   *   asset protocol and keeps working with relative paths, so './' is kept.
   *
   * Keyed on `mode` because that is what src-tauri/tauri.conf.json passes in
   * its beforeBuildCommand — no extra env var for a packager to forget.
   */
  const assetBase = mode === 'tauri' ? './' : '/'
  const apiBaseUrl = env.VITE_API_BASE_URL || 'http://127.0.0.1:8483'
  const port = parseInt(env.VITE_FRONTEND_PORT || '3015', 10)
  const appVersion = env.VITE_APP_VERSION || process.env.VITE_APP_VERSION || readAppVersion()

  return {
    base: assetBase,
    define: {
      __APP_VERSION__: JSON.stringify(appVersion),
    },
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    build: {
      rollupOptions: {
        output: {
          manualChunks: {
            markdown: ['react-markdown', 'react-syntax-highlighter', 'remark-gfm', 'remark-math', 'rehype-katex'],
            vendor: ['react', 'react-dom', 'react-router-dom'],
            ui: ['antd', '@ant-design/x', '@radix-ui/react-dialog', 'lucide-react'],
            math: ['katex'],
            animation: ['lottie-react', '@lottiefiles/dotlottie-react'],
          },
        },
      },
    },
    server: {
      host: '127.0.0.1',
      port: port,
      allowedHosts: ['localhost', '127.0.0.1'], // 本机开发仅接受本机域名
      proxy: {
        '/api': {
          target: apiBaseUrl,
          changeOrigin: true,
          rewrite: path => path.replace(/^\/api/, '/api'),
        },
        '/static': {
          target: apiBaseUrl,
          changeOrigin: true,
          rewrite: path => path.replace(/^\/static/, '/static'),
        },
      },
    },
  }
})
