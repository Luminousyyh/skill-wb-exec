/**
 * wb-exec as a DeepSeek Harness (dsh) plugin.
 *
 * Contributes exactly one thing to a profile: a `ctx.skills` provider that
 * publishes the packaged `skills/wb-exec/` tree. There is no MCP server, no
 * tool and nothing to configure at install time.
 *
 * The provider contract (`list` / `get`) is implemented here directly rather
 * than by importing an in-box package: an out-of-tree bundle that depended on
 * something like `@deepseek-ai/dsh-skill-filesystem` would install a second
 * copy that drifts from the host's.
 *
 * Plain ESM with no build step, on purpose — `dsh plugin add github:...` fetches
 * sources, not build output, and a `prepare` script would force every user to
 * allowlist a build before the first install could succeed.
 */

import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

/** Name this skill provider registers under. */
const PROVIDER_NAME = 'wb-exec'
/** Skill name used when SKILL.md declares none. */
const DEFAULT_SKILL_NAME = 'wb-exec'
/** Rank of the `bundled` discovery source in dsh's own skill provider. */
const BUNDLED_SKILL_RANK = 600

const SKILL_DIR_URL = new URL('../skills/wb-exec/', import.meta.url)
const SKILL_BODY_URL = new URL('SKILL.md', SKILL_DIR_URL)
const RESOURCE_BASE = { kind: 'directory', path: fileURLToPath(SKILL_DIR_URL) }
const INVOCATION = { modelInvocable: true, userInvocable: true }

/**
 * Read one flat key from SKILL.md's YAML frontmatter.
 *
 * SKILL.md stays the single source of truth for the skill's name and
 * description; copying either value into this file would be a second copy that
 * rots silently. The frontmatter is a handful of flat keys, so pulling in a
 * YAML parser would be more surface than the job needs.
 *
 * @param {string} body - raw SKILL.md contents.
 * @param {string} key - frontmatter key to read, such as `name`.
 * @returns {string | undefined} the unquoted value, or undefined when absent.
 */
function readFrontmatter(body, key) {
  const block = /^---\r?\n([\s\S]*?)\r?\n---/.exec(body)
  if (block === null) return undefined
  const escaped = key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const line = new RegExp('^' + escaped + ':[ \t]*(.+)$', 'm').exec(block[1])
  if (line === null) return undefined
  const raw = line[1].trim()
  if (raw.length > 1 && raw.startsWith('"') && raw.endsWith('"')) {
    return raw.slice(1, -1).replace(/\\"/g, '"').replace(/\\\\/g, '\\')
  }
  if (raw.length > 1 && raw.startsWith("'") && raw.endsWith("'")) {
    return raw.slice(1, -1).replace(/''/g, "'")
  }
  return raw
}

/**
 * Load the packaged skill, or undefined when it cannot be read.
 *
 * @param {AbortSignal | undefined} signal - lookup cancellation.
 * @returns {Promise<{ name: string, description: string, content: string } | undefined>}
 */
async function loadSkill(signal) {
  const content = await readFile(SKILL_BODY_URL, { encoding: 'utf8', signal })
  const description = readFrontmatter(content, 'description')
  if (description === undefined) return undefined
  return {
    name: readFrontmatter(content, 'name') ?? DEFAULT_SKILL_NAME,
    description,
    content,
  }
}

const provider = {
  name: PROVIDER_NAME,
  async list(options = {}) {
    const skill = await loadSkill(options.signal)
    if (skill === undefined) return []
    return [{
      name: skill.name,
      description: skill.description,
      invocation: INVOCATION,
      provider: PROVIDER_NAME,
      source: 'bundled',
      resourceBase: RESOURCE_BASE,
      rank: BUNDLED_SKILL_RANK,
      locator: SKILL_BODY_URL,
    }]
  },
  async get(_candidate, options = {}) {
    const skill = await loadSkill(options.signal)
    if (skill === undefined) return undefined
    return {
      name: skill.name,
      description: skill.description,
      invocation: INVOCATION,
      provider: PROVIDER_NAME,
      source: 'bundled',
      resourceBase: RESOURCE_BASE,
      content: skill.content,
    }
  },
}

/** Cordis plugin name; must match the `id` in dsh/cordis.patch.yml. */
export const name = 'wb-exec-skill'
/** The registry this plugin contributes to. */
export const inject = ['skills']

/**
 * Register the packaged wb-exec skill on `ctx.skills`.
 *
 * @param {import('@deepseek-ai/cordis').Context} ctx - the plugin context.
 */
export function apply(ctx) {
  ctx.skills.registerProvider(() => provider)
}