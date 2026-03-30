# Wallet Integration Guide

Reusable patterns for integrating EVM wallet connections into any web app — browser extensions (MetaMask, Rabby, Talisman, etc.), WalletConnect QR protocol, and EIP-712 typed data signing. Includes Hyperliquid-specific approveAgent flow.

---

## Architecture Overview

A wallet connect page typically has three connection paths:

1. **EIP-6963 discovered wallets** — auto-detects all installed browser extensions
2. **WalletConnect** — QR code / deep link for mobile and remote wallets
3. **Manual key entry** — fallback for API key or private key paste

All paths feed into the same signing/approval flow.

---

## EIP-6963: Browser Extension Discovery

### Why Not `window.ethereum`?

`window.ethereum` is a single global that browsers like Brave hijack. If a user has Talisman + MetaMask + Brave Wallet installed, only one will control `window.ethereum`. EIP-6963 fixes this by letting each extension announce itself independently.

### Implementation

```javascript
const discoveredWallets = new Map();

window.addEventListener('eip6963:announceProvider', (event) => {
  const { info, provider } = event.detail;
  // info = { uuid, name, icon (data URI), rdns (e.g. "xyz.talisman") }
  // provider = EIP-1193 provider object (replaces window.ethereum)
  if (!discoveredWallets.has(info.rdns)) {
    discoveredWallets.set(info.rdns, { info, provider });
    renderWalletButton(info, provider);
  }
});

// Trigger discovery — extensions respond immediately
window.dispatchEvent(new Event('eip6963:requestProvider'));
```

### Connecting with a Discovered Provider

```javascript
async function connectExtensionWallet(provider, walletName) {
  // Use the specific provider, NOT window.ethereum
  const accounts = await provider.request({ method: 'eth_requestAccounts' });
  const address = accounts[0].toLowerCase();

  // Wrap in ethers.js for signing
  const ethersProvider = new ethers.BrowserProvider(provider);
  const signer = await ethersProvider.getSigner();

  return { address, signer };
}
```

### Common rdns Values

| Wallet    | rdns                    |
|-----------|-------------------------|
| MetaMask  | `io.metamask`           |
| Rabby     | `io.rabby`              |
| Talisman  | `xyz.talisman`          |
| Brave     | `com.brave.wallet`      |
| Coinbase  | `com.coinbase.wallet`   |
| OKX       | `com.okex.wallet`       |

---

## WalletConnect Protocol

### Setup (CDN / Dynamic Import)

```javascript
// Lazy-load WalletConnect SDK when user clicks the button
const { EthereumProvider } = await import(
  'https://esm.sh/@walletconnect/ethereum-provider@2.17.0?bundle'
);

const wcProvider = await EthereumProvider.init({
  projectId: 'YOUR_PROJECT_ID',  // Free at https://cloud.reown.com
  chains: [1],                    // Ethereum mainnet
  optionalChains: [42161],        // Arbitrum
  showQrModal: true,              // Built-in QR modal UI
  qrModalOptions: { themeMode: 'dark' },
  metadata: {
    name: 'Your App',
    description: 'App description',
    url: 'https://yourapp.com',
    icons: ['https://yourapp.com/icon.png'],
  },
});

// Opens QR modal — user scans with mobile wallet
await wcProvider.connect();

const accounts = wcProvider.accounts;
const address = accounts[0].toLowerCase();

// Wrap for signing
const ethersProvider = new ethers.BrowserProvider(wcProvider);
const signer = await ethersProvider.getSigner();
```

### Requirements

- **Project ID**: Required. Free at [cloud.reown.com](https://cloud.reown.com). Can be passed as URL param or hardcoded.
- **CDN**: `esm.sh` works for dynamic imports. Load lazily (large dependency tree).
- **Cleanup**: Call `wcProvider.disconnect()` after successful flow.

---

## EIP-712 Typed Data Signing

### General Pattern

```javascript
const domain = {
  name: 'DomainName',
  version: '1',
  chainId: 42161,  // Must match the chain context
  verifyingContract: '0x0000000000000000000000000000000000000000',
};

const types = {
  'PrimaryTypeName': [
    { name: 'field1', type: 'string' },
    { name: 'field2', type: 'address' },
    { name: 'field3', type: 'uint64' },
  ],
};

const value = { field1: 'value', field2: '0x...', field3: 12345 };

const signature = await signer.signTypedData(domain, types, value);
```

### Signature Decomposition (r, s, v)

```python
sig = signature.removeprefix("0x")
r = "0x" + sig[:64]
s = "0x" + sig[64:128]
v = int(sig[128:130], 16)
if v < 27:
    v += 27
```

---

## Hyperliquid-Specific: approveAgent

### Chain ID Rules (CRITICAL)

| Network  | EIP-712 domain chainId | signatureChainId (hex) |
|----------|------------------------|------------------------|
| Mainnet  | 42161                  | `"0xa4b1"`            |
| Testnet  | 421614                 | `"0x66eee"`           |

**User-signed actions** (approveAgent, transfers, withdrawals) use the **Arbitrum chain ID**, NOT the Hyperliquid L1 chain (1337). Using 1337 will produce a valid signature that the API silently rejects.

### EIP-712 Domain (Mainnet)

```javascript
const domain = {
  name: 'HyperliquidSignTransaction',
  version: '1',
  chainId: 42161,  // Arbitrum One
  verifyingContract: '0x0000000000000000000000000000000000000000',
};
```

### EIP-712 Types

```javascript
const types = {
  'HyperliquidTransaction:ApproveAgent': [
    { name: 'hyperliquidChain', type: 'string' },
    { name: 'agentAddress', type: 'address' },
    { name: 'agentName', type: 'string' },
    { name: 'nonce', type: 'uint64' },
  ],
};

const value = {
  hyperliquidChain: 'Mainnet',
  agentAddress: '0x...',    // lowercase
  agentName: 'yourapp',     // or '' for unnamed
  nonce: timestampMs,       // int(time.time() * 1000)
};
```

### Exchange API Payload

POST to `https://api.hyperliquid.xyz/exchange`:

```json
{
  "action": {
    "type": "approveAgent",
    "hyperliquidChain": "Mainnet",
    "signatureChainId": "0xa4b1",
    "agentAddress": "0x...",
    "agentName": "yourapp",
    "nonce": 1774854944859
  },
  "nonce": 1774854944859,
  "signature": { "r": "0x...", "s": "0x...", "v": 27 },
  "vaultAddress": null
}
```

### Required Fields Checklist

- `signatureChainId` in action — **REQUIRED**. Must match EIP-712 domain chainId in hex
- `vaultAddress` at top level — **REQUIRED** even if null (Rust deserializer expects it)
- `nonce` appears in BOTH the action and top level — same value (timestamp in ms)
- `agentAddress` must be lowercase

### Agent Wallet Generation

```javascript
const agentWallet = ethers.Wallet.createRandom();
const agentAddress = agentWallet.address.toLowerCase();
const agentPrivateKey = agentWallet.privateKey;
// This key can trade but CANNOT withdraw — scoped by approveAgent
```

### Credential Storage (macOS Keychain)

```bash
# Store
security add-generic-password -s "yourapp" -a "yourapp.master_address" -w "0x..." -U
security add-generic-password -s "yourapp" -a "yourapp.agent_private_key" -w "0x..." -U
security add-generic-password -s "yourapp" -a "yourapp.agent_address" -w "0x..." -U

# Retrieve
security find-generic-password -s "yourapp" -a "yourapp.master_address" -w

# Delete
security delete-generic-password -s "yourapp" -a "yourapp.master_address"
```

---

## Local Server Pattern (Desktop Apps)

For desktop apps that need wallet connect without a hosted frontend:

1. **Serve HTML** at `http://127.0.0.1:{random_port}/`
2. **`/nonce` endpoint** — returns `{ "nonce": int(time.time() * 1000) }`
3. **`/complete` endpoint** — receives signed data, submits to chain API, stores credentials
4. **`/save-credentials`** — for manual key entry fallback
5. **`/status`** — check if already connected
6. **Auto-open browser** via `webbrowser.open(url)`
7. **Auto-shutdown** after successful connection

Pass port as URL param (`?port=12345`) so the HTML knows where to POST.

---

## Common Pitfalls

| Pitfall | Symptom | Fix |
|---------|---------|-----|
| Brave hijacks `window.ethereum` | Wrong wallet opens | Use EIP-6963 |
| Wrong chainId for Hyperliquid | 422 deserialization error | Use 42161 (Arbitrum), not 1337 |
| Missing `signatureChainId` | 422 deserialization error | Add `"0xa4b1"` to action |
| Missing `vaultAddress: null` | 422 deserialization error | Include even when not using vault |
| WalletConnect without projectId | SDK throws on init | Get free ID at cloud.reown.com |
| Signature v = 0 or 1 | API rejects signature | Normalize: `if v < 27: v += 27` |
| ethers v5 vs v6 API | `BrowserProvider` not found | v6 = `BrowserProvider`, v5 = `Web3Provider` |

---

## Dependencies

| Package | CDN URL | Purpose |
|---------|---------|---------|
| ethers.js v6 | `cdnjs.cloudflare.com/ajax/libs/ethers/6.13.4/ethers.umd.min.js` | Wallet provider, signing |
| WalletConnect | `esm.sh/@walletconnect/ethereum-provider@2.17.0?bundle` | QR code wallet connect |

---

## Reference Implementation

See `hyperbot/scripts/connect/` for a complete working example:
- `wallet_connect.html` — full UI with EIP-6963 + WalletConnect + manual key
- `server.py` — Python HTTP server with nonce, complete, save-credentials, and status endpoints
