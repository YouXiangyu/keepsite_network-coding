import { expect, test } from '@playwright/test';

const repo = 'YouXiangyu/keepsite_network-coding';
const api = `https://api.github.com/repos/${repo}/releases/latest`;
const asset = (tag: string) => ({
  name: 'keepsite.apk', size: 32, state: 'uploaded',
  browser_download_url: `https://github.com/${repo}/releases/download/${tag}/keepsite.apk`,
});
for (const width of [390, 1440]) {
  test(`anonymous download flow at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await page.route('**/api/auth/session', route => route.fulfill({ json: {
      authenticated: false, configured: true, providers: { email: true, google: false },
    } }));
    let tag = 'v2';
    await page.route(api, route => route.fulfill({ json: { tag_name: tag, body: '发行说明', assets: [asset(tag)] } }));
    await page.goto('/login');
    await page.getByRole('link', { name: '下载 Android App' }).click();
    const download = page.getByRole('link', { name: '下载最新版 APK' });
    await expect(download).toHaveAttribute('href', asset(tag).browser_download_url);
    await expect(page.getByText('发行说明')).toBeVisible();
    await page.route(asset(tag).browser_download_url, route => route.fulfill({ headers: {
      'Content-Disposition': 'attachment; filename="keepsite.apk"',
    }, body: 'test apk' }));
    const [file] = await Promise.all([page.waitForEvent('download'), download.click()]);
    expect(file.suggestedFilename()).toBe('keepsite.apk');
    tag = 'v3';
    await page.reload();
    await expect(download).toHaveAttribute('href', asset(tag).browser_download_url);
    await page.getByRole('link', { name: '返回登录页' }).click();
    await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  });
}

test('release failures and unavailable releases never offer an old APK', async ({ page }) => {
  let status = 503;
  let payload = {};
  await page.route(api, route => route.fulfill({ status, json: payload }));
  await page.goto('/download/android');
  await expect(page.getByRole('alert')).toContainText('503');
  status = 404;
  await page.getByRole('button', { name: '重试' }).click();
  await expect(page.getByRole('status')).toContainText('尚无正式版本');
  status = 200;
  for (const release of [{ draft: true }, { prerelease: true }, { assets: [] }]) {
    payload = { tag_name: 'v4', assets: [asset('v4')], ...release };
    await page.reload();
    await expect(page.getByRole('status')).toContainText('暂时无法下载');
    await expect(page.getByRole('link', { name: '下载最新版 APK' })).toHaveCount(0);
  }
});
