import { readdir, readFile } from 'node:fs/promises'
import path from 'node:path'

const [builtDir, committedDir] = process.argv.slice(2)

if (!builtDir || !committedDir) {
  console.error('Usage: bun verify_static_assets.mjs <build-dir> <committed-dir>')
  process.exit(2)
}

const hashedAssetPattern = /-([A-Za-z0-9_-]{8})(?=\.[^.]+$)/g
const textExtensions = new Set(['.css', '.html', '.js', '.json', '.map', '.svg', '.txt'])

function normalizeAssetName(name) {
  return name.replace(hashedAssetPattern, '-<hash>')
}

async function readTree(root) {
  const entries = []

  async function visit(directory) {
    const children = await readdir(directory, { withFileTypes: true })
    children.sort((left, right) => left.name.localeCompare(right.name))

    for (const child of children) {
      const absolutePath = path.join(directory, child.name)
      if (child.isDirectory()) {
        await visit(absolutePath)
      } else if (child.isFile()) {
        const relativePath = path.relative(root, absolutePath).split(path.sep).join('/')
        entries.push({ name: child.name, path: relativePath, bytes: await readFile(absolutePath) })
      }
    }
  }

  await visit(root)
  return entries
}

async function normalizedTree(root) {
  const entries = await readTree(root)
  const replacements = entries
    .map(({ name }) => [name, normalizeAssetName(name)])
    .filter(([name, normalized]) => name !== normalized)
    .sort(([left], [right]) => right.length - left.length)

  const files = new Map()
  for (const entry of entries) {
    const normalizedPath = entry.path
      .split('/')
      .map(normalizeAssetName)
      .join('/')
    if (files.has(normalizedPath)) {
      throw new Error(`Multiple assets normalize to ${normalizedPath} in ${root}`)
    }

    let bytes = entry.bytes
    if (textExtensions.has(path.extname(entry.path).toLowerCase())) {
      let content = bytes.toString('utf8')
      for (const [name, normalized] of replacements) {
        content = content.replaceAll(name, normalized)
      }
      bytes = Buffer.from(content)
    }
    files.set(normalizedPath, bytes)
  }
  return files
}

const [built, committed] = await Promise.all([
  normalizedTree(path.resolve(builtDir)),
  normalizedTree(path.resolve(committedDir)),
])

const names = [...new Set([...built.keys(), ...committed.keys()])].sort()
const differences = names.filter((name) => {
  const builtBytes = built.get(name)
  const committedBytes = committed.get(name)
  return !builtBytes || !committedBytes || !builtBytes.equals(committedBytes)
})

if (differences.length) {
  console.error('Committed static assets differ from the build after normalizing 8-character asset hashes:')
  for (const name of differences.slice(0, 20)) {
    const status = !built.has(name) ? 'missing from build' : !committed.has(name) ? 'missing from committed assets' : 'content differs'
    console.error(`  ${name}: ${status}`)
    const builtBytes = built.get(name)
    const committedBytes = committed.get(name)
    if (builtBytes && committedBytes && textExtensions.has(path.extname(name).toLowerCase())) {
      const builtText = builtBytes.toString('utf8')
      const committedText = committedBytes.toString('utf8')
      let offset = 0
      while (offset < Math.min(builtText.length, committedText.length) && builtText[offset] === committedText[offset]) {
        offset += 1
      }
      const start = Math.max(0, offset - 80)
      const end = offset + 160
      console.error(`    build:     ${JSON.stringify(builtText.slice(start, end))}`)
      console.error(`    committed: ${JSON.stringify(committedText.slice(start, end))}`)
    }
  }
  if (differences.length > 20) {
    console.error(`  ... and ${differences.length - 20} more differences`)
  }
  process.exit(1)
}

console.log(`Verified ${built.size} static assets (ignoring generated hash suffixes).`)
