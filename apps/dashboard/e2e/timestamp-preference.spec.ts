import { expect, test } from '@playwright/test'

test.use({ timezoneId: 'America/New_York' })

test('UTC preference updates another open route without losing its draft or expansion', async ({ page, context }) => {
  await page.goto('/logs?thread=run-active')
  const timestamp = page.locator('.log-time').first()
  await expect(timestamp).toContainText(/EST|EDT/)
  const local = await timestamp.textContent()
  await page.getByRole('searchbox', { name: 'Log query' }).fill('Unsaved investigation query')
  await page.getByRole('button', { name: /Toggle fields/ }).first().click()
  await expect(page.getByTestId('log-fields-row')).toBeVisible()

  const settings = await context.newPage()
  try {
    await settings.goto('/settings')
    await settings.getByRole('combobox', { name: 'Timestamp time zone' }).selectOption('UTC')
    await expect(timestamp).toContainText('UTC')
    expect(await timestamp.textContent()).not.toBe(local)
    await expect(page.getByRole('searchbox', { name: 'Log query' })).toHaveValue('Unsaved investigation query')
    await expect(page.getByTestId('log-fields-row')).toBeVisible()
    await settings.reload()
    await expect(settings.getByRole('combobox', { name: 'Timestamp time zone' })).toHaveValue('UTC')
    await settings.getByRole('combobox', { name: 'Timestamp time zone' }).selectOption('local')
    await expect(timestamp).toHaveText(local ?? '')
    await expect(page.getByRole('searchbox', { name: 'Log query' })).toHaveValue('Unsaved investigation query')
  } finally {
    await settings.close()
  }
})
