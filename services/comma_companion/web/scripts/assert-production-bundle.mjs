import { readdir, readFile } from 'node:fs/promises'
import { basename, join, relative, resolve } from 'node:path'

const outputDirectory = resolve(process.cwd(), 'dist')
const forbiddenFixtureMarkers = [
  'device-ioniq5',
  'fe7070223b',
  '100.98.247.60',
  '0000011c--116efaac12',
  'Fjellhamar',
]

async function listJavaScriptFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true })
  const files = await Promise.all(entries.map((entry) => {
    const path = join(directory, entry.name)
    if (entry.isDirectory()) return listJavaScriptFiles(path)
    return entry.isFile() && entry.name.endsWith('.js') ? [path] : []
  }))
  return files.flat()
}

const javascriptFiles = await listJavaScriptFiles(outputDirectory)
if (javascriptFiles.length === 0) {
  throw new Error(`No JavaScript bundles were found under ${outputDirectory}.`)
}

const findings = []
for (const path of javascriptFiles) {
  if (/^demo(?:[-.].*)?\.js$/i.test(basename(path))) {
    findings.push(`${relative(outputDirectory, path)}: demo chunk`)
  }
  const contents = await readFile(path, 'utf8')
  for (const marker of forbiddenFixtureMarkers) {
    if (contents.includes(marker)) {
      findings.push(`${relative(outputDirectory, path)}: ${marker}`)
    }
  }
}

if (findings.length > 0) {
  throw new Error(
    `Production bundle contains development demo data:\n${findings
      .map((finding) => `- ${finding}`)
      .join('\n')}`,
  )
}

console.log(
  `Production bundle privacy assertion passed (${javascriptFiles.length} JavaScript file${
    javascriptFiles.length === 1 ? '' : 's'
  }).`,
)
