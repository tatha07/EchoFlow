import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';
import * as SecureStore from 'expo-secure-store';
import { User, AudioClip, TabName } from '../types';

interface AuthState {
  user: User | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  login: (user: User) => void;
  logout: () => void;
  setLoading: (loading: boolean) => void;
}

interface FeedState {
  clips: AudioClip[];
  isLoading: boolean;
  error: string | null;
  hasMore: boolean;
  setClips: (clips: AudioClip[]) => void;
  addClips: (clips: AudioClip[]) => void;
  clearClips: () => void;
  toggleLike: (clipId: string) => void;
  setLoading: (loading: boolean) => void;
  setError: (error: string | null) => void;
}

interface NavigationState {
  activeTab: TabName;
  setActiveTab: (tab: TabName) => void;
}

// Custom storage for Zustand persistence
const customStorage = {
  getItem: async (name: string): Promise<string | null> => {
    try {
      return await SecureStore.getItemAsync(name);
    } catch {
      return null;
    }
  },
  setItem: async (name: string, value: string): Promise<void> => {
    try {
      await SecureStore.setItemAsync(name, value);
    } catch (error) {
      console.error('Error storing item:', error);
    }
  },
  removeItem: async (name: string): Promise<void> => {
    try {
      await SecureStore.deleteItemAsync(name);
    } catch (error) {
      console.error('Error removing item:', error);
    }
  },
};

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      user: null,
      isAuthenticated: false,
      isLoading: true,
      login: (user) => set({ user, isAuthenticated: true, isLoading: false }),
      logout: () => set({ user: null, isAuthenticated: false, isLoading: false }),
      setLoading: (loading) => set({ isLoading: loading }),
    }),
    {
      name: 'echoflow-auth',
      storage: createJSONStorage(() => customStorage),
      partialize: (state) => ({ user: state.user, isAuthenticated: state.isAuthenticated }),
    }
  )
);

export const useFeedStore = create<FeedState>((set) => ({
  clips: [],
  isLoading: false,
  error: null,
  hasMore: true,
  setClips: (clips) => set({ clips, isLoading: false }),
  addClips: (clips) =>
    set((state) => ({ clips: [...state.clips, ...clips], isLoading: false })),
  clearClips: () => set({ clips: [], hasMore: true }),
  toggleLike: (clipId) =>
    set((state) => ({
      clips: state.clips.map((clip) =>
        clip.id === clipId
          ? {
              ...clip,
              is_liked: !clip.is_liked,
              likes: clip.is_liked ? clip.likes - 1 : clip.likes + 1,
            }
          : clip
      ),
    })),
  setLoading: (loading) => set({ isLoading: loading }),
  setError: (error) => set({ error, isLoading: false }),
}));

export const useNavigationStore = create<NavigationState>((set) => ({
  activeTab: 'home',
  setActiveTab: (tab) => set({ activeTab: tab }),
}));
