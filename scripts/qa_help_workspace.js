// Run with playwright-cli run-code --filename scripts/qa_help_workspace.js
// after opening an isolated production demo tab. Uses only the active origin.
async (page) => {
  const base = new URL(page.url()).origin;
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.setViewportSize({width:1440,height:1000});
  await page.goto(base + '/help/welcome');
  await page.getByRole('heading', {name:'Tests, runs, reviews, and decisions'}).waitFor();
  const search = page.getByRole('searchbox', {name:'Search documentation', exact:true});
  await search.fill('human completion');
  const results = page.getByRole('region', {name:'Search results', exact:true});
  await results.getByRole('link', {name:'Run, review & complete phases', exact:true}).click();
  await page.getByRole('heading', {name:'Complete human work', exact:true}).waitFor();
  await page.goBack();
  if (await search.inputValue() !== 'human completion') throw new Error('Search state not restored on Back');
  await page.getByRole('button', {name:'Clear search',exact:true}).click();
  await page.getByRole('heading', {name:'Tests, runs, reviews, and decisions'}).waitFor();
  await page.getByRole('navigation', {name:'On this page',exact:true}).getByRole('link', {name:'Roles and access',exact:true}).click();
  await page.waitForFunction(() => document.activeElement?.id === 'roles-and-access');
  const anchorVisible = await page.evaluate(() => document.getElementById('roles-and-access').getBoundingClientRect().top >= document.querySelector('.main-topbar').getBoundingClientRect().bottom);
  if (!anchorVisible) throw new Error('Anchor obscured by sticky header');
  await page.locator('main').evaluate(el => el.scrollTop = 0);
  await page.getByRole('navigation', {name:'On this page',exact:true}).getByRole('link', {name:'Roles and access',exact:true}).click();
  await page.waitForFunction(() => document.getElementById('roles-and-access').getBoundingClientRect().top < innerHeight);
  await page.setViewportSize({width:390,height:844});
  await page.waitForFunction(() => document.getElementById('roles-and-access').getBoundingClientRect().top >= document.querySelector('.main-topbar').getBoundingClientRect().bottom);
  await page.setViewportSize({width:1440,height:1000});
  await page.reload();
  await page.waitForFunction(() => document.activeElement?.id === 'roles-and-access');
  await page.goto(base + '/help/api-reference?operation=listPipelines');
  const api = page.getByRole('region', {name:'Generated domain API reference',exact:true});
  await api.getByText('1 matching operations', {exact:true}).waitFor();
  await api.getByRole('combobox', {name:'Schema name',exact:true}).selectOption('StartPipelineRequest');
  await api.getByRole('heading', {name:'StartPipelineRequest',exact:true}).waitFor();
  const downloadEvent = page.waitForEvent('download');
  await api.getByRole('link', {name:'Download OpenAPI contract',exact:true}).click();
  const download = await downloadEvent;
  if (download.suggestedFilename() !== 'apex-v1.openapi.json') throw new Error('Wrong OpenAPI download');
  if (await download.failure()) throw new Error('OpenAPI download failed');
  await page.goto(base + '/help/api-reference?tag=retired-group&operation=listPipelines');
  await api.getByText('1 matching operations', {exact:true}).waitFor();
  await api.getByText(/retired-group.*not in this contract/).waitFor();
  await page.goto(base + '/help/welcome#main-content');
  await page.getByRole('heading', {name:'Roles and access',exact:true}).waitFor();
  if (await page.getByText(/That section is not in this build/).count()) throw new Error('Shell skip target reported as a missing article section');
  await page.getByRole('navigation', {name:'Primary',exact:true}).getByRole('link', {name:/^Tests/}).click();
  await page.getByRole('heading', {name:'Tests',exact:true,level:1}).waitFor();
  await page.emulateMedia({media:'print'});
  if (await page.locator('.main-topbar').evaluate(el => getComputedStyle(el).display === 'none')) throw new Error('Help print styles leaked to the Tests screen');
  await page.emulateMedia({media:'screen'});
  await page.goto(base + '/help/first-test');
  await page.getByRole('heading', {name:'Move to connected execution',exact:true}).waitFor();
  await page.getByRole('img', {name:/Offline demo launch preview/}).evaluate(img => img.decode());
  await page.goto(base + '/help/welcome');
  await page.getByRole('heading', {name:'Tests, runs, reviews, and decisions'}).waitFor();
  await page.setViewportSize({width:1440,height:1000});
  await page.screenshot({animations:'disabled',path:'output/playwright/help-desktop.png'});
  const themes = ['light','dark','solarized-dark','solarized-light','monokai-dimmed'];
  for (const theme of themes) {
    await page.evaluate(theme => document.documentElement.setAttribute('data-theme', theme), theme);
    await page.screenshot({animations:'disabled',path:'output/playwright/help-theme-'+theme+'.png'});
    const contrasts = await page.evaluate(() => {
      const luminance = value => {
        const values = value.match(/[\d.]+/g).slice(0,3).map(Number).map(v => { const x=v/255; return x<=0.04045?x/12.92:Math.pow((x+0.055)/1.055,2.4); });
        return .2126*values[0]+.7152*values[1]+.0722*values[2];
      };
      const bg=luminance(getComputedStyle(document.querySelector('.help-reading-pane')).backgroundColor);
      return ['.help-prose','.help-prose a','.help-copy button'].map(selector => { const color=luminance(getComputedStyle(document.querySelector(selector)).color);return (Math.max(bg,color)+.05)/(Math.min(bg,color)+.05); });
    });
    if (contrasts.some(ratio => ratio < 4.5)) throw new Error(theme + ' text contrast below 4.5: ' + contrasts);
  }
  await page.emulateMedia({media:'print'});
  const print = await page.evaluate(() => ({ sidebar:getComputedStyle(document.querySelector('.help-sidebar')).display, toolbar:getComputedStyle(document.querySelector('.main-topbar')).display, color:getComputedStyle(document.querySelector('.help-prose')).color, overflow:getComputedStyle(document.body).overflow }));
  if (print.sidebar !== 'none' || print.toolbar !== 'none' || print.color !== 'rgb(17, 17, 17)' || print.overflow !== 'visible') throw new Error('Print style failed: ' + JSON.stringify(print));
  await page.emulateMedia({media:'screen'});
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'));
  for (const width of [768,390]) {
    await page.setViewportSize({width,height:844});
    const overflow = await page.evaluate(() => ({root:document.documentElement.scrollWidth-innerWidth,main:document.querySelector('main').scrollWidth-document.querySelector('main').clientWidth}));
    if (overflow.root > 1 || overflow.main > 1) throw new Error('Horizontal overflow at '+width+': '+JSON.stringify(overflow));
    const badgeClipped = await page.evaluate(() => { const badge=document.querySelector('.topbar-meta-chip'); return badge && badge.scrollWidth > badge.clientWidth + 1; });
    if (badgeClipped) throw new Error('Clipped demo badge at '+width);
    await page.screenshot({animations:'disabled',path:'output/playwright/help-'+width+'.png'});
  }
  await page.getByRole('button', {name:'Open navigation',exact:true}).click();
  const dialog = page.getByRole('dialog', {name:'Navigation',exact:true});
  await dialog.getByRole('link', {name:'Help & documentation',exact:true}).waitFor();
  await page.keyboard.press('Escape');
  await page.getByRole('button', {name:'Open navigation',exact:true}).waitFor();
  await page.setViewportSize({width:1440,height:1000});
  await page.evaluate(() => document.documentElement.style.zoom='2');
  const overflow = await page.evaluate(() => document.querySelector('main').scrollWidth-document.querySelector('main').clientWidth);
  if (overflow > 1) throw new Error('Horizontal overflow with 200% zoom: '+overflow);
  await page.screenshot({animations:'disabled',path:'output/playwright/help-zoom.png'});
  await page.evaluate(() => document.documentElement.style.zoom='');
  await page.goto(base + '/runs/new/?step=constructor');
  await page.getByRole('tab', {name:'Scope',exact:true}).waitFor();
  if (await page.getByRole('link', {name:'Help for this page',exact:true}).getAttribute('href') !== '/help/scope') throw new Error('Unsupported wizard step did not fall back to Scope help');
  await page.goto(base + '/help/welcome');
  await page.getByRole('heading', {name:'Tests, runs, reviews, and decisions'}).waitFor();
  if (errors.length) throw new Error('Page errors: '+errors.join('; '));
}
