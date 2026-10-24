import os
from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.core.files.storage import default_storage
from .models import OrganizerProfile, Event, TicketTier, GalleryImage, EventAttendee, FreeAttendeeGuest, Feedback
from django.forms import inlineformset_factory
import re

###########

class HostRegistrationForm(UserCreationForm):
    account_type = forms.ChoiceField(
        choices=[
            ('individual', 'Individual'),
            ('organization', 'Organization / Company'),
        ],
        label="Account type"
    )
    organization_name = forms.CharField(
        max_length=255,
        required=False,
        label="Organization / Company name"
    )
    first_name = forms.CharField(max_length=150, label="First name")
    last_name = forms.CharField(max_length=150, label="Last name")
    email = forms.EmailField(label="Email address")
    phone = forms.CharField(
        max_length=30,
        label="Phone number",
        help_text="Include your country code, e.g. +1 470 555 1234"
    )
    city = forms.CharField(max_length=100, label="City")
    state_province = forms.CharField(max_length=100, label="State / Province")
    country = forms.CharField(max_length=100, label="Country")

    class Meta:
        model = User
        fields = [
            'account_type', 'organization_name',
            'first_name', 'last_name', 'email', 'phone',
            'city', 'state_province', 'country',
            'username', 'password1', 'password2'
        ]

    def clean_first_name(self):
        value = self.cleaned_data['first_name'].strip()
        if not re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ' -]+", value):
            raise forms.ValidationError("Enter a valid first name.")
        return value

    def clean_last_name(self):
        value = self.cleaned_data['last_name'].strip()
        if not re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ' -]+", value):
            raise forms.ValidationError("Enter a valid last name.")
        return value

    def clean_phone(self):
        value = self.cleaned_data['phone'].strip()
        if not re.fullmatch(r"\+[0-9][0-9\s().-]{7,29}", value):
            raise forms.ValidationError(
                "Enter a valid phone number with country code, e.g. +1 929 559 1111."
            )
        return value

    def clean_city(self):
        value = self.cleaned_data['city'].strip()
        if not re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ' -]+", value):
            raise forms.ValidationError("Enter a valid city.")
        return value

    def clean_state_province(self):
        value = self.cleaned_data['state_province'].strip()
        if not re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ' -]+", value):
            raise forms.ValidationError("Enter a valid state or province.")
        return value

    def clean_country(self):
        value = self.cleaned_data['country'].strip()
        if not re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ' -]+", value):
            raise forms.ValidationError("Enter a valid country.")
        return value

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get('account_type') == 'organization' and not cleaned_data.get('organization_name', '').strip():
            self.add_error('organization_name', 'Organization / Company name is required.')
        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.first_name = self.cleaned_data['first_name']
        user.last_name = self.cleaned_data['last_name']
        user.email = self.cleaned_data['email']

        if commit:
            user.save()
            OrganizerProfile.objects.update_or_create(
                user=user,
                defaults={
                    'account_type': self.cleaned_data['account_type'],
                    'organization_name': self.cleaned_data.get('organization_name', '').strip(),
                    'phone': self.cleaned_data['phone'],
                    'city': self.cleaned_data['city'],
                    'state_province': self.cleaned_data['state_province'],
                    'country': self.cleaned_data['country'],
                }
            )

        return user
##########

class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True

    def value_from_datadict(self, data, files, name):
        if hasattr(files, 'getlist'):
            files_list = files.getlist(name)
            return files_list if files_list else super().value_from_datadict(data, files, name)
        return super().value_from_datadict(data, files, name)


class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput(attrs={'accept': 'image/*', 'multiple': True}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_file_clean = super().clean
        if isinstance(data, (list, tuple)):
            cleaned_list = []
            for item in data:
                if item:
                    cleaned_list.append(single_file_clean(item, initial))
            return cleaned_list
        return single_file_clean(data, initial)


class EventForm(forms.ModelForm):
    PAGE_BG_CHOICES = [
        # Landing Page Special Backgrounds
        ('#020617', 'Landing Page Deep Space (Default)'),
        ('#0f172a', 'Landing Page Surface Dark'),
        ('#1e293b', 'Landing Page Surface Muted'),
        
        # Neutrals
        ('#f8fafc', 'Slate Light'),
        ('#ffffff', 'Pure White'),
        ('#f1f5f9', 'Cool Gray'),
        ('#f5f5f4', 'Warm Stone'),
        ('#fafaf9', 'Soft Ivory'),

        # Dark
        ('#0f172a', 'Dark Slate'),
        ('#1e293b', 'Deep Blue Gray'),
        ('#18181b', 'Charcoal'),
        ('#292524', 'Dark Stone'),
        ('#071C13', 'Emerald Dark'),
        ('#0B2B1D', 'Deep Emerald'),

        # Warm
        ('#fffbeb', 'Warm Cream'),
        ('#fef3c7', 'Soft Amber'),
        ('#fff7ed', 'Peach Cream'),
        ('#fdf2f8', 'Blush'),
        ('#fff1f2', 'Soft Rose'),

        # Gold / Luxury
        ('#E8D3A7', 'Light Gold'),
        ('#F7EED7', 'Soft Champagne'),
        ('#F5E9CC', 'Pale Gold'),
        ('#FAF3E0', 'Champagne Cream'),

        # Cool / Fresh
        ('#ecfdf5', 'Soft Emerald'),
        ('#f0fdf4', 'Mint'),
        ('#eff6ff', 'Soft Blue'),
        ('#f0f9ff', 'Soft Sky'),
        ('#eef2ff', 'Soft Indigo'),
        ('#f5f3ff', 'Soft Violet'),

        # Elegant
        ('#faf5ff', 'Lavender'),
        ('#fdf4ff', 'Soft Fuchsia'),
        ('#fce7f3', 'Elegant Pink'),
        ('#f3e8ff', 'Soft Purple'),
    ]

    SECTION_BG_CHOICES = [
        # Landing Page Card Backgrounds
        ('rgba(15, 23, 42, 0.75)', 'Landing Page Glass Dark Card'),
        ('rgba(30, 41, 59, 0.85)', 'Landing Page Glass Surface Card'),
        ('rgba(255, 255, 255, 0.03)', 'Landing Page Subtle Frosted Glass'),

        # Neutrals
        ('#ffffff', 'White Card (Default)'),
        ('#f8fafc', 'Slate White Card'),
        ('#f1f5f9', 'Light Gray Card'),
        ('#f5f5f4', 'Warm Stone Card'),
        ('#fafaf9', 'Ivory Card'),

        # Dark
        ('#1e293b', 'Dark Slate Card'),
        ('#0f172a', 'Midnight Card'),
        ('#27272a', 'Charcoal Card'),
        ('#0B2B1D', 'Deep Emerald Card'),
        ('#071C13', 'Emerald Dark Card'),

        # Warm
        ('#fef3c7', 'Warm Amber Card'),
        ('#ffedd5', 'Peach Card'),
        ('#ffe4e6', 'Soft Rose Card'),
        ('#fce7f3', 'Blush Pink Card'),

        # Gold / Metallic / Luxury
        ('#78350f', 'Deep Gold Brown'),
        ('#92400e', 'Burnt Amber'),
        ('#FFF8E1', 'Champagne Gold'),
        ('#F5E6B3', 'Pale Gold'),
        ('#E8D3A7', 'Light Gold'),
        ('#D4AF37', 'Classic Gold'),
        ('#C5A059', 'Real Gold'),
        ('#B08C46', 'Antique Gold'),
        ('#A38241', 'Dark Gold'),
        ('#8C6A2D', 'Deep Gold'),
        ('#6F531F', 'Dark Metallic Gold'),
        ('#4A3513', 'Rich Dark Gold'),

        # Emerald / Green
        ('#dcfce7', 'Soft Emerald Card'),
        ('#d1fae5', 'Mint Card'),
        ('#d9f2e6', 'Pale Emerald Card'),

        # Cool / Fresh
        ('#dbeafe', 'Soft Blue Card'),
        ('#e0f2fe', 'Sky Blue Card'),
        ('#e0e7ff', 'Soft Indigo Card'),
        ('#ede9fe', 'Soft Violet Card'),

        # Elegant
        ('#f3e8ff', 'Lavender Card'),
        ('#fae8ff', 'Soft Fuchsia Card'),
    ]

    BODY_COLOR_CHOICES = [
        # Landing Page Body Text & Subtitles
        ('#cbd5e1', 'Landing Page Slate Subtitle (Default)'),
        ('#94a3b8', 'Landing Page Muted Body Text'),
        ('#f1f5f9', 'Landing Page Bright Body Text'),

        # Neutrals
        ('#475569', 'Standard Slate'),
        ('#334155', 'Darker Slate'),
        ('#64748b', 'Medium Slate'),
        ('#52525b', 'Zinc Gray'),
        ('#44403c', 'Warm Gray'),
        ('#1e293b', 'Deep Slate'),

        # Emerald / Green
        ('#0B2B1D', 'Deep Emerald'),
        ('#14532d', 'Forest Green'),
        ('#166534', 'Deep Green'),
        ('#047857', 'Emerald'),
        ('#15803d', 'Green'),

        # Gold / Earth
        ('#78350f', 'Deep Gold Brown'),
        ('#92400e', 'Burnt Amber'),
        ('#FFF8E1', 'Champagne Gold'),
        ('#F5E6B3', 'Pale Gold'),
        ('#E8D3A7', 'Light Gold'),
        ('#D4AF37', 'Classic Gold'),
        ('#C5A059', 'Real Gold'),
        ('#B08C46', 'Antique Gold'),
        ('#A38241', 'Dark Gold'),
        ('#8C6A2D', 'Deep Gold'),
        ('#6F531F', 'Dark Metallic Gold'),
        ('#4A3513', 'Rich Dark Gold'),

        # Blues
        ('#2B5B84', 'Steel Blue'),
        ('#1e40af', 'Blue'),
        ('#1d4ed8', 'Royal Blue'),
        ('#3730a3', 'Indigo'),
        ('#4338ca', 'Deep Indigo'),

        # Warm
        ('#9a3412', 'Burnt Orange'),
        ('#b45309', 'Amber'),
        ('#991b1b', 'Deep Red'),
        ('#be123c', 'Rose'),

        # Purple
        ('#7e22ce', 'Purple'),
        ('#86198f', 'Magenta'),

        # Light — useful with dark backgrounds
        ('#94a3b8', 'Light Gray'),
        ('#cbd5e1', 'Soft Gray'),
        ('#e2e8f0', 'Off-White'),
        ('#f1f5f9', 'Very Light Gray'),
        ('#ffffff', 'Pure White'),
    ]

    MUTED_COLOR_CHOICES = [
        # Slate / Gray
        ('#94a3b8', 'Slate Muted (Default)'),
        ('#64748b', 'Medium Slate'),
        ('#6b7280', 'Neutral Gray'),
        ('#71717a', 'Zinc Gray'),
        ('#78716c', 'Warm Gray'),
        ('#475569', 'Dark Slate'),
        ('#334155', 'Deep Slate'),

        # Emerald / Green
        ('#527565', 'Muted Emerald'),
        ('#4d8068', 'Soft Emerald'),
        ('#6b8f7a', 'Sage Green'),

        # Gold / Earth
        ('#A38241', 'Dark Gold'),
        ('#B08C46', 'Antique Gold'),
        ('#9C8450', 'Muted Gold'),
        ('#8B7355', 'Warm Bronze'),

        # Light
        ('#cbd5e1', 'Soft Light Gray'),
        ('#d1d5db', 'Light Gray'),
        ('#e2e8f0', 'Pale Slate'),
        ('#f1f5f9', 'Very Light'),
        ('#f8fafc', 'Slate White'),
        ('#ffffff', 'Pure White'),

        # Colored Muted
        ('#4f46e5', 'Muted Indigo'),
        ('#047857', 'Muted Emerald'),
        ('#b45309', 'Muted Amber'),
        ('#be123c', 'Muted Rose'),
        ('#7e22ce', 'Muted Purple'),
        ('#2B5B84', 'Muted Steel Blue'),
    ]

    HEADING_COLOR_CHOICES = [
        # Landing Page Headings & Titles
        ('#ffffff', 'Landing Page Pure White Heading (Default)'),
        ('#f8fafc', 'Landing Page Soft White Heading'),
        ('#e2e8f0', 'Landing Page Off-White Heading'),

        # Neutrals / Slate
        ('#0f172a', 'Dark Slate'),
        ('#1e293b', 'Deep Slate'),
        ('#334155', 'Slate'),
        ('#18181b', 'Charcoal'),
        ('#000000', 'Pure Black'),

        # Emerald / Green
        ('#0B2B1D', 'Deep Emerald'),
        ('#071C13', 'Emerald Dark'),
        ('#14532d', 'Forest Green'),
        ('#166534', 'Deep Green'),
        ('#047857', 'Emerald'),

        # Gold / Earth
        ('#78350f', 'Deep Gold Brown'),
        ('#92400e', 'Burnt Amber'),
        ('#FFF8E1', 'Champagne Gold'),
        ('#F5E6B3', 'Pale Gold'),
        ('#E8D3A7', 'Light Gold'),
        ('#D4AF37', 'Classic Gold'),
        ('#C5A059', 'Real Gold'),
        ('#B08C46', 'Antique Gold'),
        ('#A38241', 'Dark Gold'),
        ('#8C6A2D', 'Deep Gold'),
        ('#6F531F', 'Dark Metallic Gold'),
        ('#4A3513', 'Rich Dark Gold'),

        # Blues
        ('#1e3a8a', 'Deep Blue'),
        ('#1e40af', 'Dark Blue'),
        ('#1d4ed8', 'Royal Blue'),
        ('#2B5B84', 'Steel Blue'),
        ('#312e81', 'Deep Indigo'),

        # Warm & Accent
        ('#9a3412', 'Burnt Orange'),
        ('#b45309', 'Warm Amber'),
        ('#991b1b', 'Deep Red'),
        ('#7f1d1d', 'Deep Crimson'),
        ('#be123c', 'Deep Rose'),

        # Purple
        ('#581c87', 'Deep Purple'),
        ('#7e22ce', 'Purple'),

        # Light — for dark backgrounds
        ('#f8fafc', 'Soft White'),
        ('#ffffff', 'Pure White'),
        ('#e2e8f0', 'Off-White'),
        ('#cbd5e1', 'Light Gray'),
    ]

    hero_image = forms.ImageField(
        required=False,
        widget=forms.FileInput(attrs={'accept': 'image/*'})
    )
    
    #gallery_files = MultipleFileField(required=False)

    theme_color = forms.CharField(
        required=False,
        initial='#3b82f6',
        widget=forms.TextInput(attrs={'type': 'color'})
    )

    page_bg_color = forms.ChoiceField(
        choices=PAGE_BG_CHOICES,
        required=False,
        initial='#f8fafc'
    )

    section_bg_color = forms.ChoiceField(
        choices=SECTION_BG_CHOICES,
        required=False,
        initial='#ffffff'
    )

    heading_color = forms.ChoiceField(
        choices=HEADING_COLOR_CHOICES,
        required=False,
        initial='#0f172a'
    )

    body_color = forms.ChoiceField(
        choices=BODY_COLOR_CHOICES,
        required=False,
        initial='#475569'
    )

    muted_color = forms.ChoiceField(
        choices=MUTED_COLOR_CHOICES,
        required=False,
        initial='#94a3b8'
    )

    font_family = forms.ChoiceField(
        choices=[
            ('sans-serif', 'Sans-Serif (Modern / Clean)'),
            ('serif', 'Serif (Classic / Elegant)'),
            ('mono', 'Monospace (Minimal / Tech)'),
        ],
        required=False,
        initial='sans-serif'
    )

    layout_style = forms.ChoiceField(
        choices=[
            ('centered', 'Centered Hero Card'),
            ('split', 'Split Two-Column Layout'),
            ('minimal', 'Minimalist Clean'),

            ('editorial', 'editorial and minimalist'),
            ('showcase', 'Showcase Diverse Events'),
            ('summit', 'Summit Layout'),
            ('nightlife', 'Night Events Layout'),
            ('story', 'story teller festival'),
        ],
        required=False,
        initial='centered'
    )

    show_about_section = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={
            'class': 'w-4 h-4 text-blue-600 border-slate-300 rounded focus:ring-blue-500 cursor-pointer'
        })
    )

    # about_title = forms.CharField(
    #     required=False,
    #     initial="About the Host",
    #     max_length=100,
    #     widget=forms.TextInput(attrs={
    #         'class': 'w-full px-3.5 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-blue-500'
    #     })
    # )
    guest_message = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            'rows': 3,
            'class': 'w-full px-3.5 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-blue-500'
        }),
        help_text="Write a short bio or background text about the host/celebrant."
    )

    allow_guest_messages = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={
            'class': 'w-4 h-4 text-blue-600 border-slate-300 rounded focus:ring-blue-500 cursor-pointer'
        }),
        help_text="Allow guests to leave a special message for the celebrant."
    )

    class Meta:
        model = Event
        fields = [
            'title', 'description', 'event_type', 'food_served', 'event_date', 'location_name', 'address', 'city', 'country',  'is_ticketed', 'max_capacity', 
            'show_about_section', 'about_title', 'about_text', 
            'allow_plus_ones', 'max_plus_ones_per_guest', 'allow_guest_messages', 
            'enable_qr_checkins', 'theme_settings',
            'announcement_title', 'announcement_text', 'show_announcement',
            # --- Added Post-Event Deliverables ---
            'presentation_file',
            'resource_link',
            'enable_certificates',
            'show_countdown',
        ]
        widgets = {
            'event_date': forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'),
            'address': forms.Textarea(attrs={'rows': 3}),
            'announcement_text': forms.Textarea(attrs={'rows': 3}),
            'presentation_file': forms.ClearableFileInput(attrs={
                'class': 'block w-full text-xs text-slate-500 file:mr-4 file:py-2 file:px-4 file:rounded-xl file:border-0 file:text-xs file:font-semibold file:bg-indigo-50 file:text-indigo-700 hover:file:bg-indigo-100'
            }),
            'resource_link': forms.URLInput(attrs={
                'class': 'w-full px-3 py-2 border border-slate-300 rounded-lg text-xs focus:ring-2 focus:ring-indigo-500 focus:outline-none',
                'placeholder': 'https://github.com/...'
            }),
            'enable_certificates': forms.CheckboxInput(attrs={
                'class': 'rounded bg-slate-900 border-slate-700 text-indigo-500 focus:ring-indigo-500 h-4 w-4'
            }),
            'description': forms.Textarea(attrs={
                'rows': 5,
                'placeholder': (
                    'Describe the event, its purpose, activities, '
                    'intended audience, and what guests can expect.'
                ),
            }),
            'event_type': forms.Select(),
        }
    ############

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # self.fields['location_name'].required = False #  NOW
        # self.fields['address'].required = False # NOW
        # self.fields['city'].required = False # NOW
        # self.fields['country'].required = True # NOW

        if self.data:
            color_choice_fields = ['page_bg_color', 'section_bg_color', 'heading_color', 'body_color', 'muted_color']
            for field_name in color_choice_fields:
                submitted_val = self.data.get(field_name)
                if submitted_val and re.match(r'^#([A-Fa-f0-9]{6}|[A-Fa-f0-9]{3})$', submitted_val):
                    field = self.fields.get(field_name)
                    if field and hasattr(field, 'choices'):
                        existing_values = [choice[0] for choice in field.choices]
                        if submitted_val not in existing_values:
                            # Append the custom color to choices so Django validation passes
                            field.choices = list(field.choices) + [(submitted_val, f"{submitted_val} (Custom)")]
  
        if self.instance and self.instance.pk and self.instance.theme_settings:
            theme = self.instance.theme_settings
            self.fields['theme_color'].initial = theme.get('primary_color', '#3b82f6')
            self.fields['page_bg_color'].initial = theme.get('page_bg_color', '#f8fafc')
            self.fields['section_bg_color'].initial = theme.get('section_bg_color', '#ffffff')
            self.fields['heading_color'].initial = theme.get('heading_color', '#0f172a')
            self.fields['body_color'].initial = theme.get('body_color', '#475569')
            self.fields['muted_color'].initial = theme.get('muted_color', '#94a3b8')
            self.fields['font_family'].initial = theme.get('font_family', 'sans-serif')
            self.fields['layout_style'].initial = theme.get('layout_style', 'centered')
            self.fields['show_about_section'].initial = theme.get('show_about_section', False)
            # self.fields['about_title'].initial = theme.get('about_title', 'About the Host')
            self.fields['guest_message'].initial = theme.get('guest_message', '')
        file_classes = 'block w-full text-sm text-slate-500 file:mr-4 file:py-2 file:px-4 file:rounded-lg file:border-0 file:text-sm file:font-semibold file:bg-blue-50 file:text-blue-700 hover:file:bg-blue-100 cursor-pointer'

        for name, field in self.fields.items():
            #if name in ['hero_image', 'gallery_files']:
            if name == 'hero_image':
                field.widget.attrs.update({'class': file_classes})
            elif name == 'theme_color':
                field.widget.attrs.update({'class': 'h-10 w-20 p-1 border border-slate-300 rounded-lg cursor-pointer'})
            elif name == 'about_text':
                field.widget.attrs.update({'class': 'w-full px-3.5 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500'})
            elif name == 'about_title':
                field.widget.attrs.update({'class': 'w-full px-3.5 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500'})
            else:
                field.widget.attrs.update({'class': 'w-full px-3.5 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500'})


    def save(self, commit=True):
            instance = super().save(commit=False)
            theme = instance.theme_settings or {}
            
            #existing_gallery_urls = list(theme.get('gallery_images', []))

            hero_file = self.cleaned_data.get('hero_image')
            if hero_file:
                path = default_storage.save(f"event_heroes/{hero_file.name}", hero_file)
                theme['hero_image_url'] = f"/media/{path}"

            theme['primary_color'] = self.cleaned_data.get('theme_color', '#3b82f6')
            theme['page_bg_color'] = self.cleaned_data.get('page_bg_color', '#f8fafc')
            theme['section_bg_color'] = self.cleaned_data.get('section_bg_color', '#ffffff')
            theme['heading_color'] = self.cleaned_data.get('heading_color', '#0f172a')
            theme['body_color'] = self.cleaned_data.get('body_color', '#475569')
            theme['muted_color'] = self.cleaned_data.get('muted_color', '#94a3b8')
            theme['font_family'] = self.cleaned_data.get('font_family', 'sans-serif')
            theme['layout_style'] = self.cleaned_data.get('layout_style', 'centered')
            theme['show_about_section'] = self.cleaned_data.get('show_about_section', False)
            theme['guest_message'] = self.cleaned_data.get('guest_message', '')

            # Capture organizer footer contact inputs
            existing_contacts = theme.get('organizer_contacts', {})
            theme['organizer_contacts'] = {
                'name': self.data.get('organizer_name', existing_contacts.get('name', '')) if hasattr(self, 'data') and self.data else existing_contacts.get('name', ''),
                'phone': self.data.get('organizer_phone', existing_contacts.get('phone', '')) if hasattr(self, 'data') and self.data else existing_contacts.get('phone', ''),
                'whatsapp': self.data.get('organizer_whatsapp', existing_contacts.get('whatsapp', '')) if hasattr(self, 'data') and self.data else existing_contacts.get('whatsapp', ''),
            }

            instance.theme_settings = theme
            if commit:
                instance.save()
            return instance


class GalleryImageForm(forms.ModelForm):
    class Meta:
        model = GalleryImage
        fields = ['image', 'description', 'is_post_event', 'order']
        widgets = {
            'image': forms.ClearableFileInput(attrs={
                'class': 'block w-full text-xs text-slate-500 file:mr-4 file:py-2 file:px-4 file:rounded-xl file:border-0 file:text-xs file:font-semibold file:bg-blue-50 file:text-blue-700 hover:file:bg-blue-100'
            }),
            'description': forms.TextInput(attrs={
                'class': 'w-full px-3 py-1.5 border border-slate-300 rounded-lg text-xs focus:ring-2 focus:ring-blue-500 focus:outline-none',
                'placeholder': 'Enter photo description or caption...'
            }),
            'is_post_event': forms.CheckboxInput(attrs={
                'class': 'rounded bg-slate-900 border-slate-700 text-indigo-500 focus:ring-indigo-500 h-4 w-4'
            }),
            'order': forms.NumberInput(attrs={
                'class': 'w-full px-3 py-1.5 border border-slate-300 rounded-lg text-xs focus:ring-2 focus:ring-blue-500 focus:outline-none',
                'placeholder': '0'
            }),
        }

TicketTierFormSet = inlineformset_factory(
    Event,
    TicketTier,
    fields=['name', 'description', 'price', 'capacity'],
    extra=1,
    can_delete=True
)

class EventTicketingForm(forms.ModelForm):
    class Meta:
        model = Event
        fields = ['is_ticketed']

class EventAttendeeForm(forms.ModelForm):
    PHONE_PREFIX_CHOICES = [
        ('+33', '+33 (France)'),
        ('+1', '+1 (US/CA)'),
        ('+44', '+44 (UK)'),
        ('+49', '+49 (Germany)'),
        ('+34', '+34 (Spain)'),
        ('+39', '+39 (Italy)'),
        ('+41', '+41 (Switzerland)'),
        ('+32', '+32 (Belgium)'),
        ('+31', '+31 (Netherlands)'),
        ('+351', '+351 (Portugal)'),
        ('+61', '+61 (Australia)'),
        ('+81', '+81 (Japan)'),
        ('OTHER', 'Other (Custom...)'),
    ]

    phone_prefix = forms.ChoiceField(
        choices=PHONE_PREFIX_CHOICES,
        initial='+33',
        required=False,
        widget=forms.Select(attrs={
            'id': 'id_phone_prefix_select',
            'class': 'w-full px-3 py-2.5 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:outline-none cursor-pointer'
        })
    )
    
    custom_phone_prefix = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'id': 'id_custom_phone_prefix',
            'placeholder': 'e.g. +234',
            'class': 'w-full px-3.5 py-2.5 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:outline-none hidden mt-2'
        })
    )

    class Meta:
        model = EventAttendee
        fields = ['first_name', 'last_name', 'email', 'phone', 'status', 'dietary_restrictions']

    def __init__(self, *args, **kwargs):
        self.event = kwargs.pop('event', None)
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        prefix = cleaned_data.get('phone_prefix')
        custom_prefix = cleaned_data.get('custom_phone_prefix')
        phone = cleaned_data.get('phone')
        
        # Fix: Ensure 'OTHER' string is never used as a prefix value if custom_prefix is blank
        if prefix == 'OTHER':
            active_prefix = custom_prefix.strip() if custom_prefix else ''
        else:
            active_prefix = prefix.strip() if prefix else ''
            
        if phone:
            clean_phone = phone.strip()
            
            # Strip out any accidental duplicate prefixes if re-saving
            if clean_phone.upper().startswith('OTHER'):
                clean_phone = clean_phone.replace('OTHER', '').strip()
            if prefix and clean_phone.startswith(str(prefix)):
                clean_phone = clean_phone.replace(str(prefix), '').strip()

            cleaned_data['phone'] = f"{active_prefix} {clean_phone}" if active_prefix else clean_phone
            
        return cleaned_data

class FreeAttendeeGuestForm(forms.ModelForm):
    class Meta:
        model = FreeAttendeeGuest
        fields = '__all__'  

class FeedbackForm(forms.ModelForm):
    rating = forms.ChoiceField(
        choices=[(i, f"{i} Stars") for i in range(5, 0, -1)],
        widget=forms.Select(attrs={'class': 'w-full rounded-xl border border-slate-300 p-2.5 text-sm bg-white'})
    )
    comment = forms.CharField(
        widget=forms.Textarea(attrs={
            'rows': 4, 
            'placeholder': 'Share your experience about this event...', 
            'class': 'w-full rounded-xl border border-slate-300 p-3 text-sm bg-white'
        }),
        label="Your Review / Comment"
    )

    class Meta:
        model = Feedback
        fields = ['rating', 'comment']
        