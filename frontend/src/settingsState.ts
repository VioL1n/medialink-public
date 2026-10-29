export type SettingsValues = Record<string, any>

export function copySettings(values: SettingsValues): SettingsValues {
  return JSON.parse(JSON.stringify(values))
}

// Refresh saved state without discarding edits in other settings groups.
export function reconcileDraft(previous: SettingsValues, draft: SettingsValues, next: SettingsValues, submitted: string[] = []): SettingsValues {
  const result = copySettings(draft)
  for (const key of Object.keys(next)) {
    if (submitted.includes(key) || JSON.stringify(draft[key]) === JSON.stringify(previous[key])) {
      result[key] = copySettings({value: next[key]}).value
    }
  }
  return result
}

export function settingsPatch(draft: SettingsValues, fields: readonly string[]): SettingsValues {
  return copySettings(Object.fromEntries(fields.map(key => [key, draft[key]])))
}

// A delayed response from one save must not undo another group's completed save.
export function mergeSettingsResponse(previous: SettingsValues, next: SettingsValues, fields: string[]): SettingsValues {
  const affected = new Set(fields)
  if (affected.has('tmdb_credential')) { affected.add('tmdb_configured'); affected.add('tmdb_auth_type') }
  if (['llm_api_key','llm_base_url','llm_model'].some(key => affected.has(key))) {
    affected.add('llm_key_saved'); affected.add('llm_configured')
  }
  if (affected.has('llm_api_key')) affected.add('llm_enabled')
  if (affected.has('telegram_api_hash')) affected.add('telegram_hash_saved')
  return copySettings({...previous, ...Object.fromEntries([...affected].filter(key => Object.hasOwn(next, key)).map(key => [key, next[key]]))})
}

export function reconcileSourceSelection(previous: string[], selected: string[], next: string[]): string[] {
  return next.filter(id => selected.includes(id) || !previous.includes(id))
}
