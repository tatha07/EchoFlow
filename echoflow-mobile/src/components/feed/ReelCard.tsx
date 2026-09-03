import React, { useRef, useEffect } from 'react';
import { View, StyleSheet, Dimensions, TouchableOpacity, Text } from 'react-native';
import { Video, ResizeMode, AVPlaybackStatus } from 'expo-av';
import { LinearGradient } from 'expo-linear-gradient';
import { Ionicons } from '@expo/vector-icons';
import { AudioClip } from '../../types';
import { colors, spacing, typography } from '../../constants/theme';

interface ReelCardProps {
  clip: AudioClip;
  isActive: boolean;
  onLike: () => void;
  onComment: () => void;
  onShare: () => void;
  onFollow?: () => void;
}

const { height: SCREEN_HEIGHT } = Dimensions.get('window');

export const ReelCard: React.FC<ReelCardProps> = ({
  clip,
  isActive,
  onLike,
  onComment,
  onShare,
  onFollow,
}) => {
  const videoRef = useRef<Video>(null);
  const [isPlaying, setIsPlaying] = React.useState(false);
  const [showControls, setShowControls] = React.useState(true);

  useEffect(() => {
    if (isActive) {
      videoRef.current?.playAsync();
      setIsPlaying(true);
    } else {
      videoRef.current?.pauseAsync();
      setIsPlaying(false);
    }
  }, [isActive]);

  const handlePlayPause = async () => {
    if (isPlaying) {
      await videoRef.current?.pauseAsync();
    } else {
      await videoRef.current?.playAsync();
    }
    setIsPlaying(!isPlaying);
  };

  const handleDoubleTap = () => {
    if (!clip.is_liked) {
      onLike();
    }
  };

  const handleLongPress = () => {
    setShowControls(false);
  };

  const handlePressOut = () => {
    setTimeout(() => setShowControls(true), 500);
  };

  return (
    <View style={styles.container}>
      {/* Background gradient aura */}
      <LinearGradient
        colors={[colors.surfaceElevated, colors.surfaceMidnight]}
        style={styles.background}
      />

      {/* Video/Audio Player */}
      <TouchableOpacity
        activeOpacity={1}
        onPress={handlePlayPause}
        onLongPress={handleLongPress}
        onPressOut={handlePressOut}
        delayLongPress={300}
        style={styles.playerContainer}
      >
        <Video
          ref={videoRef}
          source={{ uri: clip.hls_playlist_url }}
          style={styles.video}
          resizeMode={ResizeMode.COVER}
          isLooping
          shouldPlay={isActive}
          useNativeControls={false}
          onError={(e) => console.log('Video error:', e)}
          onPlaybackStatusUpdate={(status: AVPlaybackStatus) => {
            if ('isPlaying' in status) {
              setIsPlaying(status.isPlaying);
            }
          }}
        />

        {/* Play/Pause indicator */}
        {!isPlaying && showControls && (
          <View style={styles.playIndicator}>
            <Ionicons name="play-circle" size={80} color="rgba(255,255,255,0.8)" />
          </View>
        )}
      </TouchableOpacity>

      {/* Interaction Rail (Right Side) */}
      {showControls && (
        <View style={styles.interactionRail}>
          {/* Profile Avatar + Follow */}
          <View style={styles.interactionItem}>
            <TouchableOpacity style={styles.avatarContainer} onPress={onFollow}>
              <View style={styles.avatar}>
                <Ionicons name="person" size={24} color={colors.textHighContrast} />
              </View>
              <View style={styles.followBadge}>
                <Ionicons name="add" size={12} color={colors.surfaceMidnight} />
              </View>
            </TouchableOpacity>
          </View>

          {/* Like Button */}
          <TouchableOpacity style={styles.interactionItem} onPress={onLike}>
            <Ionicons
              name={clip.is_liked ? 'heart' : 'heart-outline'}
              size={32}
              color={clip.is_liked ? colors.terracotta : colors.textHighContrast}
            />
            <Text style={styles.interactionCount}>{formatNumber(clip.likes)}</Text>
          </TouchableOpacity>

          {/* Comment Button */}
          <TouchableOpacity style={styles.interactionItem} onPress={onComment}>
            <Ionicons name="chatbubble-outline" size={28} color={colors.textHighContrast} />
            <Text style={styles.interactionCount}>{formatNumber(clip.comment_count)}</Text>
          </TouchableOpacity>

          {/* Share Button */}
          <TouchableOpacity style={styles.interactionItem} onPress={onShare}>
            <Ionicons name="share-outline" size={28} color={colors.textHighContrast} />
            <Text style={styles.interactionCount}>{formatNumber(clip.shares)}</Text>
          </TouchableOpacity>

          {/* Spinning Vinyl Disc */}
          <View style={[styles.vinylDisc, isPlaying && styles.spinning]}>
            <View style={styles.vinylCenter}>
              <Ionicons name="musical-notes" size={20} color={colors.terracotta} />
            </View>
          </View>
        </View>
      )}

      {/* Metadata Overlay (Bottom Left) */}
      {showControls && (
        <LinearGradient
          colors={['transparent', 'rgba(0,0,0,0.8)']}
          style={styles.metadataGradient}
        >
          <View style={styles.metadataContainer}>
            <Text style={styles.creatorName}>@{clip.creator_name}</Text>
            <Text style={styles.clipTitle} numberOfLines={2}>
              {clip.title || 'Untitled Audio'}
            </Text>
            {clip.category && (
              <Text style={styles.categoryTag}>#{clip.category}</Text>
            )}
          </View>
        </LinearGradient>
      )}
    </View>
  );
};

// Helper function to format numbers (e.g., 1200 -> 1.2K)
const formatNumber = (num: number): string => {
  if (num >= 1000000) {
    return `${(num / 1000000).toFixed(1)}M`;
  }
  if (num >= 1000) {
    return `${(num / 1000).toFixed(1)}K`;
  }
  return num.toString();
};

// Simple Text component wrapper
const Text: React.FC<any> = ({ style, ...props }) => (
  <View style={style}>
    {/* In real app, use actual Text component */}
  </View>
);

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: colors.surfaceMidnight,
  },
  background: {
    ...StyleSheet.absoluteFillObject,
  },
  playerContainer: {
    flex: 1,
    justifyContent: 'center',
    alignItems: 'center',
  },
  video: {
    width: '100%',
    height: '100%',
  },
  playIndicator: {
    position: 'absolute',
  },
  interactionRail: {
    position: 'absolute',
    right: spacing.md,
    bottom: 100,
    alignItems: 'center',
  },
  interactionItem: {
    alignItems: 'center',
    marginVertical: spacing.md,
  },
  avatarContainer: {
    position: 'relative',
  },
  avatar: {
    width: 48,
    height: 48,
    borderRadius: 24,
    backgroundColor: colors.surfaceElevated,
    justifyContent: 'center',
    alignItems: 'center',
    borderWidth: 2,
    borderColor: colors.textHighContrast,
  },
  followBadge: {
    position: 'absolute',
    bottom: -4,
    right: -4,
    width: 20,
    height: 20,
    borderRadius: 10,
    backgroundColor: colors.terracotta,
    justifyContent: 'center',
    alignItems: 'center',
  },
  interactionCount: {
    color: colors.textHighContrast,
    fontSize: 12,
    marginTop: 4,
    fontWeight: '600',
  },
  vinylDisc: {
    width: 48,
    height: 48,
    borderRadius: 24,
    backgroundColor: colors.surfaceContainer,
    justifyContent: 'center',
    alignItems: 'center',
    marginTop: spacing.lg,
    borderWidth: 2,
    borderColor: colors.borderSubtle,
  },
  vinylCenter: {
    width: 20,
    height: 20,
    borderRadius: 10,
    backgroundColor: colors.terracotta,
    justifyContent: 'center',
    alignItems: 'center',
  },
  spinning: {
    transform: [{ rotate: '360deg' }],
  },
  metadataGradient: {
    position: 'absolute',
    bottom: 0,
    left: 0,
    right: 80,
    paddingTop: 60,
    paddingBottom: spacing.xl,
    paddingHorizontal: spacing.lg,
  },
  metadataContainer: {
    gap: spacing.sm,
  },
  creatorName: {
    color: colors.textHighContrast,
    fontSize: 16,
    fontWeight: '700',
  },
  clipTitle: {
    color: colors.textMuted,
    fontSize: 14,
  },
  categoryTag: {
    color: colors.sageGreen,
    fontSize: 12,
    fontWeight: '600',
  },
});

export default ReelCard;
