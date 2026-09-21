from django.contrib import admin
from .models import Event, GalleryImage, OrganizerProfile, RSVP, RSVPGuest


class GuestInline(admin.TabularInline):
    model = RSVP
    extra = 1

class GalleryImageInline(admin.TabularInline):
    model = GalleryImage
    extra = 1
    fields = ['image', 'description', 'order']

@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ('title', 'host', 'event_date', 'slug', 'created_at')
    search_fields = ('title', 'host__username', 'slug')
    prepopulated_fields = {'slug': ('title',)}
    inlines = [GuestInline, GalleryImageInline]

@admin.register(RSVP)
class GuestAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'email', 'event', 'status', 'plus_ones_count', 'dietary_restrictions', 'guest_message')
    list_filter = ('status', 'event')
    search_fields = ('first_name', 'last_name', 'email', 'event__title')
    list_per_page = 25

@admin.register(OrganizerProfile)
class OrganizerProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'created_at')  
    search_fields = ('user__username', 'user__email')
    list_filter = ('created_at',)