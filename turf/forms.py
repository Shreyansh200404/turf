from django import forms

from .models import Feedback, Turf


class TurfForm(forms.ModelForm):
    slot_price = forms.IntegerField(min_value=1)

    class Meta:
        model = Turf
        fields = (
            'name',
            'image',
            'slot_price',
            'description',
            'address',
            'contact_phone',
            'contact_email',
            'is_active',
        )
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3}),
            'address': forms.Textarea(attrs={'rows': 2}),
        }


class FeedbackForm(forms.ModelForm):
    class Meta:
        model = Feedback
        fields = ('rating', 'message')
        widgets = {
            'rating': forms.Select(attrs={'class': 'form-select'}),
            'message': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 4,
                'maxlength': 2000,
                'placeholder': 'Tell us about your experience',
            }),
        }
        labels = {
            'rating': 'Your rating',
            'message': 'Your feedback',
        }


class FeedbackAdminForm(forms.ModelForm):
    class Meta:
        model = Feedback
        fields = ('status', 'admin_response')
        widgets = {
            'status': forms.Select(attrs={'class': 'form-select'}),
            'admin_response': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 3,
                'placeholder': 'Write a response to the user (optional)',
            }),
        }
