export type Sub2ModelOption = [string, string]

// The empty value is intentional: the backend reads the current Sub2API
// group's model list at upload time, so newly added safe models work without
// a frontend release. Free accounts must not select Sol or its gpt-5.6 alias.
export const SUB2_FREE_MODEL_OPTIONS: Sub2ModelOption[] = [
  ['', '自动支持最新模型（按 Free 组实时读取）'],
  ['gpt-5.4', 'GPT-5.4'],
  ['gpt-5.4-mini', 'GPT-5.4 Mini'],
  ['gpt-5.3-codex-spark', 'GPT-5.3 Codex Spark'],
  ['gpt-5.2', 'GPT-5.2'],
]

export const SUB2_FREE_MODEL_SELECT_OPTIONS = SUB2_FREE_MODEL_OPTIONS.map(
  ([value, label]) => ({ value, label }),
)

export function isFreeSolModel(model: unknown): boolean {
  const normalized = String(model || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
  if (!normalized) return false
  if (['gpt-5-6', 'gpt-5-6-sol', 'gpt-5-6-sol-latest', 'gpt-5-6-codex-sol'].includes(normalized)) {
    return true
  }
  return /(^|-)sol(-|$)/.test(normalized)
}

export function normalizeFreeSub2Model(model: unknown): string {
  const value = String(model || '').trim()
  return isFreeSolModel(value) ? '' : value
}
