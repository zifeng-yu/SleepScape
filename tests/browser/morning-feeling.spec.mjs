import { test, expect } from "@playwright/test";

test.beforeEach(async ({request}) => {
    expect((await (await request.get("/api/v1/status")).json()).demo).toBe(true);
});

test("empty night supports entry, edit, retry, reload and backfill on mobile", async ({page, request}) => {
    await page.setViewportSize({width: 320, height: 780});
    const day = "2026-05-17";
    const path = `/api/v1/nights/${day}`;
    expect((await request.get(path)).status()).toBe(404);
    await page.goto(`/sleep?view=night&date=${day}`);
    const group = page.getByRole("group", {name:"晨起睡感"});
    const choice = label => group.getByRole("button", {name:label, exact:true});
    await expect(group.locator('[aria-pressed="true"]')).toHaveCount(0);
    await choice("一般").click();
    await expect(choice("一般")).toHaveAttribute("aria-pressed","true");
    await page.route("**/morning-feeling", route => route.fulfill({status:503,body:"{}"}));
    await choice("较好").click();
    await expect(group).toContainText("保存未确认");
    await expect(choice("一般")).toHaveAttribute("aria-pressed","true");
    await page.unroute("**/morning-feeling");
    await choice("较好").focus();
    await page.keyboard.press("Enter");
    await expect(choice("较好")).toHaveAttribute("aria-pressed","true");
    await page.reload();
    await expect(choice("较好")).toHaveAttribute("aria-pressed","true");
    await page.getByRole("button",{name:"上一晚"}).click();
    await choice("较差").click();
    await expect(choice("较差")).toHaveAttribute("aria-pressed","true");
    await page.getByRole("button",{name:"下一晚"}).click();
    await expect(choice("较好")).toHaveAttribute("aria-pressed","true");
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
});

test("ratings stay usable when night detail fails and a prior date read arrives late", async ({page}) => {
    let release;
    const pending = new Promise(resolve => {release=resolve;});
    await page.route("**/2026-05-20/morning-feeling", async route => {
        await pending;
        await route.fulfill({json:{feeling:"good"}}).catch(()=>{});
    });
    await page.route("**/nights/2026-05-21", route=>route.fulfill({status:503,body:"{}"}));
    await page.goto("/sleep?view=night&date=2026-05-20");
    await page.getByRole("button",{name:"下一晚"}).click();
    release();
    await expect(page.getByText("数据暂时无法读取",{exact:true})).toBeVisible();
    const choice=page.getByRole("button",{name:"一般",exact:true});
    await choice.click();
    await expect(choice).toHaveAttribute("aria-pressed","true");
});

for (const [source, day] of [["Garmin", "2026-11-08"], ["hardware", "2026-11-10"], ["both", "2026-11-09"]]) {
    test(`rating on ${source} night survives refresh`, async ({page}) => {
        await page.goto(`/sleep?view=night&date=${day}`);
        const button = page.getByRole("group",{name:"晨起睡感"}).getByRole("button",{name:"一般"});
        await button.click();
        await expect(button).toHaveAttribute("aria-pressed","true");
        await page.reload();
        await expect(button).toHaveAttribute("aria-pressed","true");
    });
}
