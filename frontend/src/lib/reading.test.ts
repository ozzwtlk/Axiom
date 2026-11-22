import { expect, test } from "vitest";
import { summarizeReading, visibleReading } from "./reading";

test("only eligible sections affect book and chapter totals", () => {
  expect(summarizeReading(["intro", "body"], { exercises: 100 })).toEqual({ touched: false, finished: false, percent: 0 });
  expect(summarizeReading(["intro", "body"], { intro: 0 })).toEqual({ touched: true, finished: false, percent: 0 });
  expect(summarizeReading(["intro", "body"], { intro: 100, exercises: 100 })).toEqual({ touched: true, finished: false, percent: 50 });
  expect(summarizeReading(["intro", "body"], { intro: 100, body: 100 })).toEqual({ touched: true, finished: true, percent: 100 });
});

test("peeking next section is ignored and reaching the end completes the section", () => {
  expect(visibleReading(550, 1600, 800)).toBeNull();
  expect(visibleReading(100, 1600, 800)).toBe(45);
  expect(visibleReading(-200, 750, 800)).toBe(100);
  expect(visibleReading(-400, 50, 800)).toBeNull();
});
