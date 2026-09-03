import apiClient, { tokenStorage } from './client';
import { API_ENDPOINTS } from '../constants/api';
import {
  AudioClip,
  Comment,
  FeedResponse,
  LoginCredentials,
  RegisterCredentials,
  AuthTokens,
  ProfileData,
  ShareEvent,
  User,
} from '../types';

// ==================== AUTH ====================

export const authApi = {
  async login(credentials: LoginCredentials): Promise<AuthTokens> {
    const response = await apiClient.post<AuthTokens>(API_ENDPOINTS.LOGIN, credentials);
    return response.data;
  },

  async register(credentials: RegisterCredentials): Promise<AuthTokens> {
    const response = await apiClient.post<AuthTokens>(API_ENDPOINTS.REGISTER, credentials);
    return response.data;
  },

  async logout(): Promise<void> {
    try {
      const tokens = await tokenStorage.getTokens();
      if (tokens?.refresh) {
        await apiClient.post(API_ENDPOINTS.LOGOUT, { refresh: tokens.refresh });
      }
    } finally {
      await tokenStorage.clearTokens();
    }
  },

  async refreshToken(refreshToken: string): Promise<AuthTokens> {
    const response = await apiClient.post<AuthTokens>(API_ENDPOINTS.TOKEN_REFRESH, {
      refresh: refreshToken,
    });
    return response.data;
  },
};

// ==================== FEED ====================

export const feedApi = {
  async getFeed(): Promise<FeedResponse> {
    const response = await apiClient.get<FeedResponse>(API_ENDPOINTS.FEED);
    return response.data;
  },

  async getSuggestions(category?: string): Promise<AudioClip[]> {
    const params = category ? { category } : {};
    const response = await apiClient.get<AudioClip[]>(API_ENDPOINTS.SUGGESTIONS, { params });
    return response.data;
  },
};

// ==================== CLIPS ====================

export const clipsApi = {
  async upload(formData: FormData): Promise<{ message: string; clip_id: string; status: string }> {
    const response = await apiClient.post(API_ENDPOINTS.CLIPS, formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
    });
    return response.data;
  },

  async getUserClips(userId: number): Promise<AudioClip[]> {
    const response = await apiClient.get<AudioClip[]>(`${API_ENDPOINTS.PROFILE}${userId}/clips/`);
    return response.data;
  },

  async toggleLike(clipId: string): Promise<{ status: 'liked' | 'unliked' }> {
    const response = await apiClient.post(`${API_ENDPOINTS.INTERACTIONS}${clipId}/toggle-like/`);
    return response.data;
  },

  async logTelemetry(
    clipId: string,
    actionType: 'view' | 'like' | 'share' | 'skip',
    watchTimeMs: number
  ): Promise<void> {
    await apiClient.post(`${API_ENDPOINTS.INTERACTIONS}${clipId}/log-telemetry/`, {
      action_type: actionType,
      watch_time_ms: watchTimeMs,
    });
  },

  async registerSkip(
    clipId: string,
    listenDurationMs: number,
    reelPositionMs: number
  ): Promise<void> {
    await apiClient.post(`${API_ENDPOINTS.INTERACTIONS}${clipId}/register-skip/`, {
      listen_duration_ms: listenDurationMs,
      reel_position_ms: reelPositionMs,
      reel_id: clipId,
    });
  },
};

// ==================== COMMENTS ====================

export const commentsApi = {
  async getComments(clipId: string): Promise<Comment[]> {
    const response = await apiClient.get<Comment[]>(API_ENDPOINTS.COMMENTS, {
      params: { clip: clipId },
    });
    return response.data;
  },

  async createComment(clipId: string, text: string, parentId?: number): Promise<Comment> {
    const response = await apiClient.post<Comment>(API_ENDPOINTS.COMMENTS, {
      clip: clipId,
      text,
      parent: parentId,
    });
    return response.data;
  },

  async deleteComment(commentId: number): Promise<void> {
    await apiClient.delete(`${API_ENDPOINTS.COMMENTS}${commentId}/`);
  },
};

// ==================== SOCIAL ====================

export const socialApi = {
  async getInbox(): Promise<ShareEvent[]> {
    const response = await apiClient.get<ShareEvent[]>(`${API_ENDPOINTS.SHARE}inbox/`);
    return response.data;
  },

  async sendShare(clipId: string, receiverId: number): Promise<void> {
    await apiClient.post(`${API_ENDPOINTS.SHARE}${clipId}/send-share/`, {
      receiver_id: receiverId,
    });
  },

  async findUser(username: string): Promise<User | null> {
    try {
      const response = await apiClient.get<User>(`${API_ENDPOINTS.SHARE}find-user/`, {
        params: { username },
      });
      return response.data;
    } catch {
      return null;
    }
  },

  async markShareAsRead(shareId: number): Promise<void> {
    await apiClient.post(`${API_ENDPOINTS.SHARE}${shareId}/mark-read/`);
  },

  async deleteShare(shareId: number): Promise<void> {
    await apiClient.delete(`${API_ENDPOINTS.SHARE}${shareId}/share-delete/`);
  },

  async getUnreadCount(): Promise<{ unread: number }> {
    const response = await apiClient.get<{ unread: number }>(`${API_ENDPOINTS.SHARE}unread-count/`);
    return response.data;
  },

  async toggleFollow(userId: number): Promise<{ status: 'followed' | 'unfollowed' }> {
    const response = await apiClient.post(`${API_ENDPOINTS.FOLLOW}${userId}/toggle-follow/`);
    return response.data;
  },
};

// ==================== PROFILE ====================

export const profileApi = {
  async getOwnProfile(): Promise<ProfileData> {
    const response = await apiClient.get<ProfileData>(`${API_ENDPOINTS.PROFILE}me/`);
    return response.data;
  },

  async updateProfile(data: { username?: string; profile_picture?: File }): Promise<ProfileData> {
    const formData = new FormData();
    if (data.username) formData.append('username', data.username);
    if (data.profile_picture) formData.append('profile_picture', data.profile_picture);

    const response = await apiClient.patch<ProfileData>(`${API_ENDPOINTS.PROFILE}me/update/`, formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
    });
    return response.data;
  },

  async getUserProfile(userId: number): Promise<ProfileData> {
    const response = await apiClient.get<ProfileData>(`${API_ENDPOINTS.PROFILE}${userId}/`);
    return response.data;
  },

  async initializeTags(selectedTags: string[]): Promise<{ status: string }> {
    const response = await apiClient.post(API_ENDPOINTS.TAGS_INITIALIZE, {
      selected_tags: selectedTags,
    });
    return response.data;
  },
};

export default {
  auth: authApi,
  feed: feedApi,
  clips: clipsApi,
  comments: commentsApi,
  social: socialApi,
  profile: profileApi,
};
