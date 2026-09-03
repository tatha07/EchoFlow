/**
 * EchoFlow Design Tokens - Instagram for Audio
 * Based on the Product & Design Specification
 */

import { Platform } from 'react-native';

export const colors = {
  // Canvas Base
  surfaceMidnight: '#121416',
  surfaceContainer: '#1E2022',
  surfaceElevated: '#2A2C2E',
  
  // Accents
  terracotta: '#E8A87C',
  sageGreen: '#AAD0B1',
  honeyGold: '#F1CE6D',
  
  // Text
  textHighContrast: '#E2E2E5',
  textMuted: '#D5C3B9',
  textSecondary: '#A8A8AB',
  
  // Borders & Dividers
  borderSubtle: '#333537',
  borderLight: '#444648',
  
  // Status
  success: '#4ADE80',
  error: '#F87171',
  warning: '#FBBF24',
  info: '#60A5FA',
  
  // Overlay
  overlayDark: 'rgba(0, 0, 0, 0.6)',
  overlayLight: 'rgba(255, 255, 255, 0.1)',
  
  // Gradient backgrounds for audio visualizers
  gradientPrimary: ['#E8A87C', '#F1CE6D'],
  gradientSecondary: ['#AAD0B1', '#60A5FA'],
  gradientWarm: ['#E8A87C', '#F59E0B'],
  gradientCool: ['#60A5FA', '#AAD0B1'],
};

export const typography = {
  fontFamily: {
    heading: Platform.select({ ios: 'System', default: 'Roboto' }),
    body: Platform.select({ ios: 'System', default: 'Roboto' }),
    mono: 'monospace',
  },
  fontSize: {
    xs: 12,
    sm: 14,
    md: 16,
    lg: 18,
    xl: 20,
    '2xl': 24,
    '3xl': 32,
    '4xl': 40,
  },
  fontWeight: {
    normal: '400',
    medium: '500',
    semibold: '600',
    bold: '700',
  },
};

export const spacing = {
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  xl: 20,
  '2xl': 24,
  '3xl': 32,
  '4xl': 40,
  '5xl': 48,
  '6xl': 64,
};

export const borderRadius = {
  none: 0,
  sm: 4,
  md: 8,
  lg: 12,
  xl: 16,
  '2xl': 20,
  '3xl': 24,
  full: 9999,
};

export const shadows = {
  none: {
    shadowColor: 'transparent',
    shadowOffset: { width: 0, height: 0 },
    shadowOpacity: 0,
    shadowRadius: 0,
    elevation: 0,
  },
  sm: {
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 1 },
    shadowOpacity: 0.2,
    shadowRadius: 2,
    elevation: 2,
  },
  md: {
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 2 },
    shadowOpacity: 0.25,
    shadowRadius: 4,
    elevation: 4,
  },
  lg: {
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 4 },
    shadowOpacity: 0.3,
    shadowRadius: 8,
    elevation: 8,
  },
};

export const animation = {
  duration: {
    fast: 150,
    normal: 300,
    slow: 500,
  },
  easing: {
    snap: [0.16, 1, 0.3, 1] as [number, number, number, number],
    spring: [0.175, 0.885, 0.32, 1.275] as [number, number, number, number],
  },
};

export const layout = {
  tabBarHeight: 80,
  headerHeight: 60,
  storiesHeight: 100,
  reelCardAspectRatio: 9 / 16,
  minReelHeight: 500,
};

// Legacy exports for compatibility
export const Colors = {
  light: {
    text: colors.textHighContrast,
    background: colors.surfaceMidnight,
    backgroundElement: colors.surfaceContainer,
    backgroundSelected: colors.surfaceElevated,
    textSecondary: colors.textMuted,
    primary: colors.terracotta,
    secondary: colors.sageGreen,
  },
  dark: {
    text: colors.textHighContrast,
    background: colors.surfaceMidnight,
    backgroundElement: colors.surfaceContainer,
    backgroundSelected: colors.surfaceElevated,
    textSecondary: colors.textMuted,
    primary: colors.terracotta,
    secondary: colors.sageGreen,
  },
} as const;

export type ThemeColor = keyof typeof Colors.light & keyof typeof Colors.dark;

export const Fonts = Platform.select({
  ios: {
    sans: 'System',
    serif: 'ui-serif',
    rounded: 'ui-rounded',
    mono: 'ui-monospace',
  },
  android: {
    sans: 'Roboto',
    serif: 'serif',
    rounded: 'normal',
    mono: 'monospace',
  },
  default: {
    sans: 'Roboto',
    serif: 'serif',
    rounded: 'normal',
    mono: 'monospace',
  },
});

export const Spacing = spacing;
export const BottomTabInset = Platform.select({ ios: 50, android: 80 }) ?? 0;
export const MaxContentWidth = 800;

export default {
  colors,
  typography,
  spacing,
  borderRadius,
  shadows,
  animation,
  layout,
};
