import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const DEFAULT_ORDER_DEDUP_TTL_MS = 120_000;
const DEFAULT_ORDER_DEDUP_PATH = path.join(os.tmpdir(), "finus-kis-order-dedup.json");

export class DuplicateOrderError extends Error {
  constructor(entry) {
    super("중복 주문 방지를 위해 동일 주문 재요청을 차단했습니다. 잠시 후 주문 내역을 확인하세요.");
    this.name = "DuplicateOrderError";
    this.entry = entry;
  }
}

export function createOrderDedupKey({
  accountNo,
  orderEnv,
  stockCode,
  side,
  quantity,
  price,
  orderType,
}) {
  const normalizedOrderType = String(orderType ?? "LIMIT").trim().toUpperCase();
  const payload = {
    accountNo: String(accountNo ?? "").trim(),
    orderEnv: String(orderEnv ?? "demo").trim().toLowerCase(),
    stockCode: String(stockCode ?? "").trim(),
    side: String(side ?? "").trim().toUpperCase(),
    quantity: String(quantity ?? "").trim(),
    price: normalizedOrderType === "MARKET" ? "0" : String(price ?? 0).trim(),
    orderType: normalizedOrderType,
  };

  return crypto.createHash("sha256").update(JSON.stringify(payload)).digest("hex");
}

function parsePositiveInteger(value, fallback, name) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    if (value !== undefined && value !== "") {
      console.error(
        `${name}=${value} 는 양의 정수(ms)가 아니어서 기본값 ${fallback}ms를 사용합니다.`,
      );
    }
    return fallback;
  }
  return parsed;
}

export class OrderDedupStore {
  constructor({
    filePath = process.env.KIS_ORDER_DEDUP_PATH || DEFAULT_ORDER_DEDUP_PATH,
    ttlMs = parsePositiveInteger(
      process.env.KIS_ORDER_DEDUP_TTL_MS,
      DEFAULT_ORDER_DEDUP_TTL_MS,
      "KIS_ORDER_DEDUP_TTL_MS",
    ),
    now = () => Date.now(),
  } = {}) {
    this.filePath = filePath;
    this.ttlMs = ttlMs;
    this.now = now;
  }

  reserve(key, request) {
    const now = this.now();
    const ledger = this.#readLedger(now);
    const existing = ledger[key];
    if (existing && Number(existing.expiresAt) > now) {
      throw new DuplicateOrderError(existing);
    }

    const entry = {
      status: "in_flight",
      reservedAt: now,
      expiresAt: now + this.ttlMs,
      request,
    };
    ledger[key] = entry;
    this.#writeLedger(ledger);
    return entry;
  }

  markSucceeded(key, response) {
    const now = this.now();
    const ledger = this.#readLedger(now);
    const existing = ledger[key];
    if (!existing) return;

    ledger[key] = {
      ...existing,
      status: "succeeded",
      completedAt: now,
      response,
    };
    this.#writeLedger(ledger);
  }

  release(key) {
    const ledger = this.#readLedger(this.now());
    if (!ledger[key]) return;
    delete ledger[key];
    this.#writeLedger(ledger);
  }

  #readLedger(now) {
    let ledger = {};
    try {
      ledger = JSON.parse(fs.readFileSync(this.filePath, "utf8"));
    } catch (error) {
      if (error.code !== "ENOENT") {
        console.error(`KIS order dedup ledger read failed: ${error.message}`);
      }
    }

    if (!ledger || typeof ledger !== "object" || Array.isArray(ledger)) {
      return {};
    }

    return Object.fromEntries(
      Object.entries(ledger).filter(([, entry]) => Number(entry?.expiresAt) > now),
    );
  }

  #writeLedger(ledger) {
    fs.mkdirSync(path.dirname(this.filePath), { recursive: true });
    fs.writeFileSync(this.filePath, JSON.stringify(ledger), { mode: 0o600 });
  }
}
