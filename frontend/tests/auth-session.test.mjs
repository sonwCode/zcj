import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const utilsSource = await readFile(resolve(root, 'src/lib/utils.ts'), 'utf8')
const appSource = await readFile(resolve(root, 'src/App.tsx'), 'utf8')

test('API helpers use the HttpOnly cookie session contract', () => {
  assert.equal(utilsSource.includes("credentials: 'include'"), true)
  assert.equal(utilsSource.includes('localStorage'), false)
  assert.equal(utilsSource.includes('Authorization'), false)
})

test('login flow does not persist or return a bearer token to the browser', () => {
  assert.equal(appSource.includes('data.token'), false)
  assert.equal(appSource.includes('setAuthToken(data.token)'), false)
  assert.equal(appSource.includes('Authorization: Bearer'), false)
})
