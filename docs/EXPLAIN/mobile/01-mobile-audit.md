# EchoFlow Mobile — Security & Architecture Audit

**Date:** 2026-09-08
**Scope:** `mobile/` directory — Expo 52 / React Native 0.76 / TypeScript
**Auditor:** Automated code audit (source-code only, no runtime analysis)

---

## 1. Architecture Overview

```
App.tsx
├── AuthProvider          (src/context/AuthContext.tsx)
├── PlayerProvider        (src/context/PlayerContext.tsx)
│   ├── FeedScreen        (vertical reel paging, FlatList + pagingEnabled)
│   ├── ExploreScreen     (category pills + FlatList cards)
│   ├── UploadScreen      (native mic recording, form, compliance checkbox)
│   ├── InboxScreen       (ShareEvent list, play shared clip)
│   └── ProfileScreen     (own/public profile, tabs, logout)
├── AudioVisualizer       (animated equalizer bars)
├── CommentModal          (bottom-sheet comment list + input)
└── ShareModal            (in-app share + native share sheet)
```

**State management:** React Context (no external state library).
**Navigation:** `@react-navigation/native` bottom tabs.
**Audio:** `expo-av` with `Audio.Sound` (not streaming — downloads HLS playlist URI).
**Persistence:** `@react-native-async-storage/async-storage` for JWT tokens + user profile.
**API:** Custom `fetch` wrapper (`src/services/api.ts`) with auto token refresh.

---

## 2. Security Findings

### HIGH — JWT tokens stored in AsyncStorage
`src/services/api.ts:17-19` — Access and refresh tokens stored in unencrypted `AsyncStorage`. On rooted/jailbroken devices or compromised React Native bridge, tokens are extractable.

**Recommendation:** Use `expo-secure-store` (`SecureStore`) instead of `AsyncStorage` for auth tokens.

### MEDIUM — No certificate pinning
`src/services/api.ts:15` — API base URL is `https://ais-dev-ra6pa3urcinkopihtdgpz3-557708310129.asia-southeast1.run.app`. Traffic is TLS-encrypted but vulnerable to MITM on compromised devices.

**Recommendation:** Implement certificate pinning via `expo-cert-pinner` or `react-native-ssl-pinning`.

### MEDIUM — No input sanitization on comments
`src/components/CommentModal.tsx:58` — `commentsAPI.postComment(clipId, newText.trim())` sends raw user text. No length validation beyond `maxLength={280}` in the UI; no backend-style sanitization on the client.

**Recommendation:** Match the backend's comment sanitization (null-byte/control-char stripping from `serializers.py:350-365`) in the client or rely on backend validation only (acceptable if backend is the source of truth).

### LOW — `autoCapitalize="none"` missing on username input
`src/components/ShareModal.tsx:88` — `autoCapitalize="none"` is set on the recipient input, but `src/screens/UploadScreen.tsx:183` — the title `TextInput` lacks it. Minor UX issue.

### LOW — Hardcoded API base URL in source
`src/services/api.ts:15` — Production URL is committed. The README instructs developers to change it, but the default is a live dev endpoint.

**Recommendation:** Use `expo-constants` to read `API_BASE_URL` from `app.json` extra config, or an env-var approach (e.g., `metro-config` env plugin).

---

## 3. Code Quality & Correctness

### 3.1 Memory Leaks

**PlayerContext.tsx:118-138** — `useEffect` sets audio player callbacks but cleanup function only nulls the callbacks; it does not unload the `Audio.Sound` instance. Navigating away from Feed while audio plays leaves the sound object alive.

```tsx
return () => {
  mobileAudioPlayer.setStatusCallback(null);
  mobileAudioPlayer.setTrackFinishedCallback(null);
};
// Missing: mobileAudioPlayer.unload() or pause
```

**UploadScreen.tsx:36-42** — `useEffect` cleanup calls `recording.stopAndUnloadAsync()` but only if `recording` is set. If the component unmounts during recording, the effect dependency `[recording]` may not fire if `recording` is updated in the same render cycle.

### 3.2 Race Conditions

**api.ts:69-108** — `refreshAccessToken` uses a module-level `refreshPromise` to deduplicate concurrent refresh calls. This is correct, but the promise is never cleared on failure — if refresh fails, `refreshPromise` stays non-null and subsequent 401s skip retry entirely.

```ts
// After a failed refresh, refreshPromise is nulled in finally (line 103).
// But: the 401 branch (line 131) does not retry if refresh returns null.
// Result: user is silently logged out on first 401, but subsequent 401s
// will also fail (tokens already cleared). This is acceptable behavior
// but could be confusing — consider a explicit "session expired" UI signal.
```

### 3.3 State Inconsistency

**FeedScreen.tsx:61-77** — `loadFeed` calls `setQueue(items)` and `playClip(items[0])` if no `currentClip`. But `playClip` is from `PlayerContext` and updates `currentClip` asynchronously. If the user scrolls rapidly, `onViewableItemsChanged` can fire before `setQueue` completes, playing a clip not in the queue.

**Recommendation:** Derive the queue from the clips list in the PlayerContext rather than maintaining a separate `queue` state in PlayerContext.

### 3.4 Missing Error Boundaries

No React Error Boundary wraps the tab navigator. A crash in any screen (e.g., malformed API response) brings down the entire app.

### 3.5 Type Safety Gaps

- `FeedScreen.tsx:37` — `({ navigation }: any)` uses `any` type.
- `ExploreScreen.tsx:26` — same `any` pattern.
- `InboxScreen.tsx:16`, `UploadScreen.tsx:26`, `ProfileScreen.tsx:17` — all use `any`.
- `RootTabParamList` in `types/index.ts` defines `Profile: { userId?: number } | undefined` but the screen accesses `route.params?.userId` without null-check on `route.params`.

### 3.6 Unused Dependencies

`package.json` — `expo-linear-gradient` and `expo-splash-screen` are not imported anywhere in the source. `lucide-react-native` imports only 5 icons but the full library is bundled.

---

## 4. API Integration Gaps

| Endpoint | Client Coverage | Gap |
|----------|----------------|-----|
| `GET /feed/` | ✅ `feedAPI.getFeed` | No pagination — loads all results at once |
| `GET /suggestions/` | ✅ `feedAPI.getSuggestions` | No pagination |
| `POST /interactions/{id}/toggle-like/` | ✅ | — |
| `POST /interactions/{id}/register-skip/` | ✅ | Called only on skip-next, not on manual skip |
| `POST /interactions/{id}/log-telemetry/` | ✅ | Fire-and-forget; no retry; 1s threshold may lose short listens |
| `GET /comments/?clip=` | ✅ `commentsAPI.getComments` | No pagination; no submit retry |
| `POST /comments/` | ✅ `commentsAPI.postComment` | No optimistic update; list refetch needed |
| `GET /share/inbox/` | ✅ `shareAPI.getInbox` | No mark-as-read endpoint called |
| `GET /share/unread-count/` | ✅ `shareAPI.getUnreadCount` | Unread count never displayed in UI |
| `POST /share/{id}/send-share/` | ✅ `shareAPI.sendShare` | — |
| `POST /clips/` | ✅ `uploadAPI.uploadAudio` | No progress callback; no cancellation |
| `GET /profile/me/` | ✅ `profileAPI.getOwnProfile` | — |
| `GET /profile/{id}/` | ✅ `profileAPI.getPublicProfile` | — |
| `POST /follow/{id}/toggle-follow/` | ✅ `profileAPI.toggleFollow` | Never called from any screen |

**Not implemented:** `/auth/register/`, `/auth/login/`, `/auth/token/refresh/` (handled by API client), `/legal/compliance/`, `/auth/consent/withdraw/`, `/auth/data/export/`, `/auth/data/delete/`.

---

## 5. Performance Concerns

1. **FlatList `pagingEnabled` + `snapToInterval`** — `FeedScreen.tsx:254-275`. Each page is `REEL_HEIGHT = SCREEN_HEIGHT - 130`. With `keyExtractor={item.id}` and no `getItemLayout`, the list recalculates layout on every render. Acceptable for small feeds (<50 clips) but degrades with large lists.

2. **AudioVisualizer animations** — `AudioVisualizer.tsx:21-42`. Creates `barCount` (default 20) `Animated.Value` instances per clip. When switching clips, old animations are stopped but new ones are created. No animation pool — potential GC pressure on rapid clip switching.

3. **ExploreScreen category pills** — `ExploreScreen.tsx:16-24`. Categories are hardcoded in the client. Adding a new category requires an app update, not a backend config change.

4. **No image caching** — Clip artwork (if added later) would need `expo-image` or similar; currently no image layer in the app.

---

## 6. Testing Gaps

- **Zero test files** in `mobile/`. No unit tests, no integration tests, no E2E.
- No test for token refresh flow (the `refreshAccessToken` deduplication logic).
- No test for audio player state transitions (load → play → pause → seek → flush telemetry).
- No test for API error handling (401, 500, network timeout).
- No test for FeedScreen scroll/paging behavior.

**Recommendation:** Add Jest + React Native Testing Library for unit tests; Detox or Maestro for E2E.

---

## 7. Compliance & Regulatory

- **DPDP consent:** `UploadScreen.tsx:34` — `consentAccepted` defaults to `true`. The checkbox is pre-checked, which may violate DPDP Act 2023 §11 (explicit consent must be affirmative, not pre-ticked).
- **Minor consent:** No age gate in the mobile app. `User.is_minor` and `parent_email` exist in the backend model but the mobile registration flow is not visible in the app source (likely handled by a separate auth screen not in this repo snapshot).
- **Content moderation:** The upload screen shows a compliance checkbox but does not display the actual terms/conditions text.

---

## 8. Build & Deployment

- `app.json` — `scheme: "echoflow"` for deep linking. No deep link handlers registered in `App.tsx`.
- `app.json` — `icon`, `splash`, `adaptiveIcon` reference `./assets/` which don't exist in the repo (`.gitignore` may exclude them, or they're missing).
- No `eas.json` or EAS Build config — pure Expo Go development or bare workflow.
- No CI/CD pipeline for the mobile app (GitHub Actions workflows exist for Django only).
- `node_modules` is 520+ directories — not gitignored in the find output but should be in `.gitignore`.

---

## 9. Summary Scorecard

| Area | Rating | Notes |
|------|--------|-------|
| Architecture | ⚠️ Fair | Context-based state works but lacks scalability |
| Security | 🔴 Poor | AsyncStorage tokens, no pinning, hardcoded URL |
| Code Quality | ⚠️ Fair | Type `any` abuse, memory leak risks, no error boundaries |
| API Coverage | ✅ Good | All backend endpoints have client wrappers |
| Performance | ⚠️ Fair | Acceptable for current scale; no virtualization beyond FlatList |
| Testing | 🔴 None | Zero test coverage |
| Compliance | ⚠️ Partial | DPDP consent pre-checked; minor age gate missing |
| Deployment | 🔴 None | No CI/CD, no build config, missing assets |

---

## 10. Priority Recommendations

1. **P0** — Move JWT tokens to `expo-secure-store`.
2. **P0** — Add error boundary around the tab navigator.
3. **P1** — Fix `refreshPromise` leak: ensure it resets on failure (already done in finally, but add explicit "session expired" UI).
4. **P1** — Unload `Audio.Sound` in PlayerContext cleanup.
5. **P1** — Replace `any` types with proper route params types.
6. **P2** — Add pagination to feed/suggestions APIs.
7. **P2** — Uncheck `consentAccepted` default on UploadScreen.
8. **P2** — Add EAS Build config + CI pipeline.
9. **P2** — Write unit tests for `api.ts` (token refresh, retry logic) and `audioPlayer.ts` (state transitions).
10. **P3** — Certificate pinning, deep link handlers, image caching.

---

*Audit covers all files under `mobile/src/` as of 2026-09-08. No runtime or penetration testing performed.*
