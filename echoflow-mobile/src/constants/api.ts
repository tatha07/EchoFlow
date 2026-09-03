// EchoFlow API Configuration
// Connect to your existing backend

const API_BASE_URL = process.env.EXPO_PUBLIC_API_URL || 'http://localhost:8000';

export const API_ENDPOINTS = {
  // Auth
  LOGIN: `${API_BASE_URL}/auth/login/`,
  REGISTER: `${API_BASE_URL}/auth/register/`,
  TOKEN_REFRESH: `${API_BASE_URL}/auth/token/refresh/`,
  LOGOUT: `${API_BASE_URL}/auth/logout/`,
  
  // Feed
  FEED: `${API_BASE_URL}/feed/`,
  SUGGESTIONS: `${API_BASE_URL}/suggestions/explore/`,
  
  // Clips
  CLIPS: `${API_BASE_URL}/clips/`,
  INTERACTIONS: `${API_BASE_URL}/interactions/`,
  
  // Comments
  COMMENTS: `${API_BASE_URL}/comments/`,
  
  // Social
  SHARE: `${API_BASE_URL}/share/`,
  FOLLOW: `${API_BASE_URL}/follow/`,
  
  // Profile
  PROFILE: `${API_BASE_URL}/profile/`,
  
  // Tags
  TAGS_INITIALIZE: `${API_BASE_URL}/tags/initialize/`,
} as const;

export default API_BASE_URL;
