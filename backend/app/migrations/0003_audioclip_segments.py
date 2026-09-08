from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('app', '0002_merged_scraper_flags'),
    ]

    operations = [
        # DECISION: Three fields added — group_id (UUID, shared across
        # segments of one source item), segment_index (0-based), and
        # segment_count (total N segments in the group, denormalized for
        # fast "show all parts" queries). One AudioClip row per
        # segment. The old uploader truncated a 4h LibriVox audiobook
        # to 5min; the new uploader splits and saves all N pieces.
        # group_id + segment_index let the feed/show pages list
        # "part 1 of 7" without a separate Segment table.
        migrations.AddField(
            model_name='audioclip',
            name='group_id',
            field=models.UUIDField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='audioclip',
            name='segment_index',
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='audioclip',
            name='segment_count',
            field=models.IntegerField(blank=True, null=True),
        ),
    ]
