from django import forms

from .models import Turf


class TurfForm(forms.ModelForm):
    slot_price = forms.IntegerField(min_value=1)

    class Meta:
        model = Turf
        fields = (
            'name',
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
