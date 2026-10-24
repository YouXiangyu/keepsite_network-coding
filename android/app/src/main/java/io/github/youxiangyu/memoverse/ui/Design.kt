package io.github.youxiangyu.memoverse.ui

import android.graphics.Typeface
import android.os.Build

/** Design tokens; see docs/android-development/DESIGN.md before adding or changing a value. */
object Design {
    enum class Type(val size: Float, val weight: Weight, val lineSpacing: Float) {
        Display(28f, Weight.Bold, 1.15f),
        Title(20f, Weight.Bold, 1.2f),
        Subtitle(16f, Weight.Medium, 1.25f),
        Body(15f, Weight.Regular, 1.35f),
        Label(13f, Weight.Medium, 1.3f),
        Caption(12f, Weight.Regular, 1.3f),
        Button(16f, Weight.Medium, 1f),
        TextButton(15f, Weight.Medium, 1f),
        Nav(11f, Weight.Medium, 1f),
    }
    /** Derived from the system default so CJK fallback fonts receive the same weight as Latin text. */
    enum class Weight(val typeface: Typeface) {
        Regular(Typeface.DEFAULT),
        Medium(if (Build.VERSION.SDK_INT >= 28) Typeface.create(Typeface.DEFAULT, 500, false) else Typeface.create("sans-serif-medium", Typeface.NORMAL)),
        Bold(Typeface.create(Typeface.DEFAULT, Typeface.BOLD)),
    }
    object Space {
        const val XS = 4
        const val S = 8
        const val M = 12
        const val L = 16
        const val PAGE = 20
        const val XL = 24
        const val XXL = 32
    }
    object Radius {
        const val IMAGE = 10
        const val CONTROL = 12
        const val PANEL = 16
        const val SHEET = 22
    }
    object Size {
        const val TOUCH = 48
        const val SEARCH = 48
        const val INPUT = 52
        const val BUTTON = 52
        const val ICON = 24
        const val INDICATOR = 20
        const val PLACEHOLDER_ICON = 36
        const val NAV_ITEM = 56
        const val NAV_ADD = 48
        const val PREVIEW = 220
        const val PROGRESS = 6
        const val AVATAR = 56
        const val INFO_LABEL = 72
        const val CONTENT_MAX = 640
        const val DISABLED_ALPHA = 0.4f
    }
}
