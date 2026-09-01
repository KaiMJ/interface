/**
 * Seed data for the mock back office, deterministic by construction: transaction
 * histories come from a seeded PRNG keyed on the account number, so the same account
 * always shows the same rows and a checkpoint asserting against them is not flaky.
 *
 * Balances live in a module-level ledger so a transfer visibly moves money. None of it
 * is real.
 */

export type AccountKind = "Checking" | "Savings" | "Money Market" | "Certificate";
export type AccountStatus = "Active" | "Frozen" | "Dormant";

export interface Account {
  number: string;
  kind: AccountKind;
  nickname: string;
  opened: string;
  status: AccountStatus;
}

export interface Txn {
  date: string;
  posted: string;
  description: string;
  category: string;
  amount: number; // negative = debit
}

export interface Member {
  id: string;
  name: string;
  since: string;
  branch: string;
  phone: string;
  /** Requires elevated entitlement; the app returns a permission denial. */
  restricted?: boolean;
  accounts: Account[];
}

export const MEMBERS: Member[] = [
  {
    id: "12345",
    name: "Dolores Chen",
    since: "2011-04-18",
    branch: "Riverside — 004",
    phone: "(503) 555-0119",
    accounts: [
      { number: "29883", kind: "Checking", nickname: "Everyday Checking", opened: "2011-04-18", status: "Active" },
      { number: "13455", kind: "Savings", nickname: "Primary Savings", opened: "2011-04-18", status: "Active" },
      { number: "13460", kind: "Money Market", nickname: "MM Reserve", opened: "2018-09-02", status: "Active" },
    ],
  },
  {
    id: "22841",
    name: "Marcus Webb",
    since: "2019-11-02",
    branch: "Downtown — 001",
    phone: "(503) 555-0164",
    accounts: [
      { number: "30117", kind: "Checking", nickname: "Free Checking", opened: "2019-11-02", status: "Active" },
      { number: "30118", kind: "Savings", nickname: "Rainy Day", opened: "2020-01-15", status: "Active" },
    ],
  },
  {
    id: "30992",
    // No savings account at all: a "get the savings balance" capability must return a
    // typed business outcome here, not a crash and not a wrong number.
    name: "Priya Raghunathan",
    since: "2023-06-27",
    branch: "Eastgate — 007",
    phone: "(503) 555-0188",
    accounts: [
      { number: "41220", kind: "Checking", nickname: "Basic Checking", opened: "2023-06-27", status: "Active" },
    ],
  },
  {
    id: "44100",
    // Restricted: exists, is findable, and viewing it is denied — a different result
    // from "not found".
    name: "Alan Osei",
    since: "2008-02-11",
    branch: "Downtown — 001",
    phone: "(503) 555-0133",
    restricted: true,
    accounts: [
      { number: "51001", kind: "Checking", nickname: "Business Operating", opened: "2008-02-11", status: "Active" },
      { number: "51002", kind: "Savings", nickname: "Business Reserve", opened: "2008-02-11", status: "Active" },
    ],
  },
  {
    id: "57310",
    name: "Ruth Okonkwo-Fairbairn",
    // A long name, on purpose: it wraps to two lines in the detail header and pushes
    // everything below it down — variance within an unchanged version.
    since: "2016-08-30",
    branch: "Riverside — 004",
    phone: "(503) 555-0102",
    accounts: [
      { number: "60455", kind: "Checking", nickname: "Joint Checking", opened: "2016-08-30", status: "Active" },
      { number: "60456", kind: "Savings", nickname: "Vacation Fund", opened: "2016-08-30", status: "Dormant" },
      { number: "60457", kind: "Certificate", nickname: "18mo CD", opened: "2024-03-11", status: "Active" },
    ],
  },
];

const SEED_BALANCES: Record<string, number> = {
  "29883": 4820.19,
  "13455": 18204.55,
  "13460": 41002.0,
  "30117": 712.04,
  "30118": 2050.0,
  "41220": 95.12,
  "51001": 128455.71,
  "51002": 64300.0,
  "60455": 3311.87,
  "60456": 15.0,
  "60457": 25000.0,
};

/** Mutable ledger. Reset from /dev. */
let ledger: Record<string, number> = { ...SEED_BALANCES };

export function balanceOf(accountNumber: string): number {
  return ledger[accountNumber] ?? 0;
}

/** Available differs from balance by holds — a distinction real teller screens
 *  make and a naive extraction gets wrong. */
export function availableOf(accountNumber: string): number {
  const held = HOLDS[accountNumber] ?? 0;
  return Math.max(0, balanceOf(accountNumber) - held);
}

const HOLDS: Record<string, number> = {
  "29883": 150.0,
  "51001": 2500.0,
};

export function applyTransfer(from: string, to: string, amount: number): void {
  ledger[from] = round2(balanceOf(from) - amount);
  ledger[to] = round2(balanceOf(to) + amount);
}

export function resetLedger(): void {
  ledger = { ...SEED_BALANCES };
}

export function findMember(id: string): Member | undefined {
  return MEMBERS.find((m) => m.id === id.trim());
}

export function searchMembers(query: string): Member[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  return MEMBERS.filter(
    (m) => m.id === q || m.name.toLowerCase().includes(q),
  );
}

export function findAccount(number: string): { member: Member; account: Account } | undefined {
  for (const m of MEMBERS) {
    const a = m.accounts.find((x) => x.number === number.trim());
    if (a) return { member: m, account: a };
  }
  return undefined;
}

export function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

export function money(n: number): string {
  return n.toLocaleString("en-US", { style: "currency", currency: "USD" });
}

// --- transaction history ---------------------------------------------------

const MERCHANTS: [string, string][] = [
  ["ACME CORP PAYROLL", "Deposit"],
  ["BLUE RIDGE FUEL #221", "Fuel"],
  ["CITY UTILITIES AUTOPAY", "Utilities"],
  ["NORTHGATE GROCERY", "Groceries"],
  ["MERIDIAN CU TRANSFER", "Transfer"],
  ["PACIFIC WIRELESS", "Telecom"],
  ["ST. VINCENT MEDICAL GRP", "Medical"],
  ["HARBORVIEW PROPERTY MGMT", "Rent"],
  ["ATM WITHDRAWAL — RIVERSIDE", "Cash"],
  ["CROSSROADS HARDWARE & SUPPLY CO", "Retail"],
  ["INTEREST PAID", "Interest"],
  ["SERVICE CHARGE", "Fee"],
];

/** mulberry32 — small, deterministic, good enough for fixture data. */
function prng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function transactionsFor(accountNumber: string, count = 22): Txn[] {
  const rand = prng(Number(accountNumber));
  const out: Txn[] = [];
  const start = new Date("2026-08-14T00:00:00Z");
  for (let i = 0; i < count; i++) {
    const [description, category] = MERCHANTS[Math.floor(rand() * MERCHANTS.length)];
    const credit = category === "Deposit" || category === "Interest";
    const magnitude = credit ? 400 + rand() * 2600 : 4 + rand() * 480;
    const d = new Date(start.getTime() - i * 86400000 * (1 + Math.floor(rand() * 2)));
    const posted = new Date(d.getTime() + 86400000);
    out.push({
      // MM/DD/YYYY, what these systems actually render; a normalizer assuming ISO
      // mis-sorts.
      date: fmtUS(d),
      posted: fmtUS(posted),
      description,
      category,
      amount: round2(credit ? magnitude : -magnitude),
    });
  }
  return out;
}

function fmtUS(d: Date): string {
  const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(d.getUTCDate()).padStart(2, "0");
  return `${mm}/${dd}/${d.getUTCFullYear()}`;
}

export const TELLER = { username: "teller01", password: "demo-password-not-a-real-one" };

/** Above this, a transfer is refused as a business outcome, not an error. */
export const DAILY_TRANSFER_LIMIT = 5000;
