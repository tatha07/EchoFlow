// EchoFlow Type Definitions

export interface User {
  id: number;
  username: string;
  profile_picture?: string;
  followers_count?: number;
  following_count?: number;
  uploads_count?: number;
  date_joined?: string;
}

export interface AudioClip {
  id: string;
  title: string;
  creator_name: string;
  creator_id: number;
  category?: string;
  hls_playlist_url: string;
  likes: number;
  shares: number;
  skips: number;
  comment_count: number;
  is_liked: boolean;
  duration_ms?: number;
  created_at?: string;
  status?: 'pending' | 'processing' | 'ready' | 'failed';
}

export interface Comment {
  id: number;
  clip: string;
  author_username: string;
  parent?: number;
  text: string;
  likes: number;
  reply_count: number;
  created_at: string;
}

export interface ShareEvent {
  id: number;
  sender_name: string;
  clip: AudioClip;
  clip_title: string;
  clip_hls_url: string;
  created_at: string;
  is_read: boolean;
}

export interface FeedResponse {
  next?: string;
  queue_health?: number;
  degraded?: boolean;
  results: AudioClip[];
}

export interface AuthTokens {
  access: string;
  refresh: string;
}

export interface LoginCredentials {
  username: string;
  password: string;
}

export interface RegisterCredentials {
  username: string;
  email: string;
  password: string;
}

export interface ProfileData {
  id: number;
  username: string;
  profile_picture?: string;
  followers_count: number;
  following_count: number;
  uploads_count: number;
  date_joined: string;
  liked_clips?: AudioClip[];
}

export interface Story {
  id: string;
  user: User;
  clip: AudioClip;
  created_at: string;
  expires_at: string;
  is_viewed: boolean;
}

export type TabName = 'home' | 'explore' | 'studio' | 'inbox' | 'profile';

export interface NavigationState {
  activeTab: TabName;
}
