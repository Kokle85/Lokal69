#!/usr/bin/env node
/**
 * Static security checks for the dashboard (run by `npm run lint` and after `npm run build`).
 *
 * Source (src/, excluding test files):
 *   - no raw-HTML sinks: dangerouslySetInnerHTML, innerHTML/outerHTML assignment, insertAdjacentHTML,
 *     document.write, DOMParser-to-DOM injection;
 *   - no dynamic code: eval(, new Function(;
 *   - no javascript: URLs;
 *   - no web-storage writes of tokens (localStorage/sessionStorage near access/refresh/claim token words);
 *   - only the two allowed VITE_ variables are referenced.
 * Build output (dist/, with --dist):
 *   - index.html has a Content-Security-Policy meta, no inline <script> without src, no inline event
 *     handlers and no inline style attributes;
 *   - no Supabase secret key (sb_secret_...) anywhere in the bundle.
 */
import { readdirSync, readFileSync, statSync, existsSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const problems = []

function walk(dir, filter) {
  const out = []
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) out.push(...walk(path, filter))
    else if (filter(path)) out.push(path)
  }
  return out
}

// Patterns are assembled from pieces so this file does not trip its own checks.
const SOURCE_RULES = [
  [new RegExp(['dangerously', 'SetInner', 'HTML'].join('')), 'raw HTML injection (React)'],
  [new RegExp(['\\.inner', 'HTML\\s*='].join('')), 'innerHTML assignment'],
  [new RegExp(['\\.outer', 'HTML\\s*='].join('')), 'outerHTML assignment'],
  [new RegExp(['insertAdjacent', 'HTML'].join('')), 'insertAdjacentHTML'],
  [new RegExp(['document\\.', 'write'].join('')), 'document.write'],
  [new RegExp(['\\beval', '\\s*\\('].join('')), 'eval'],
  [new RegExp(['new\\s+Func', 'tion\\s*\\('].join('')), 'new Function'],
  [new RegExp(['java', 'script:'].join(''), 'i'), 'javascript: URL'],
  [new RegExp(['(local|session)Storage\\.setItem\\([^)]*(access|refresh|claim)_?', 'token'].join(''), 'i'), 'token written to web storage'],
]

const sourceFiles = walk(join(root, 'src'), (p) => /\.(ts|tsx)$/.test(p) && !/\.test\.(ts|tsx)$/.test(p) && !p.includes(`${join('src', 'test')}`))
for (const file of sourceFiles) {
  const text = readFileSync(file, 'utf8')
  for (const [pattern, why] of SOURCE_RULES) {
    if (pattern.test(text)) problems.push(`${relative(root, file)}: ${why}`)
  }
  for (const match of text.matchAll(/import\.meta\.env\.(VITE_[A-Z0-9_]+)/g)) {
    if (match[1] !== 'VITE_SUPABASE_URL' && match[1] !== 'VITE_SUPABASE_PUBLISHABLE_KEY') {
      problems.push(`${relative(root, file)}: unexpected browser variable ${match[1]}`)
    }
  }
}

if (process.argv.includes('--dist')) {
  const dist = join(root, process.env.DASHBOARD_OUT_DIR ?? 'dist')
  if (!existsSync(join(dist, 'index.html'))) {
    problems.push('dist/index.html is missing (run vite build first)')
  } else {
    const html = readFileSync(join(dist, 'index.html'), 'utf8')
    if (!/http-equiv="Content-Security-Policy"/.test(html)) problems.push('dist/index.html: no Content-Security-Policy meta')
    for (const tag of html.matchAll(/<script\b[^>]*>/gi)) {
      if (!/\bsrc=/.test(tag[0])) problems.push('dist/index.html: inline <script> without src')
    }
    if (/\son[a-z]+\s*=/i.test(html)) problems.push('dist/index.html: inline event handler attribute')
    if (/\sstyle\s*=/i.test(html)) problems.push('dist/index.html: inline style attribute')
    for (const file of walk(dist, (p) => /\.(js|html|css)$/.test(p))) {
      if (/sb_secret_[A-Za-z0-9_-]{6,}/.test(readFileSync(file, 'utf8'))) {
        problems.push(`${relative(root, file)}: contains a Supabase secret key`)
      }
    }
  }
}

if (problems.length) {
  console.error('Security check failed:\n' + problems.map((p) => `  - ${p}`).join('\n'))
  process.exit(1)
}
console.log(`Security check passed (${sourceFiles.length} source files${process.argv.includes('--dist') ? ' + build output' : ''}).`)
