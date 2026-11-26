package io.github.youxiangyu.keepsite

import android.icu.text.Transliterator
import java.util.LinkedHashMap
import java.util.Locale

/** Shared fuzzy-search rules for ticket titles and cinema names. */
internal object TicketSearch {
    private data class SearchForms(
        val original: String,
        val fullPinyin: String,
        val initials: String,
    )

    private val transliterator = ThreadLocal.withInitial {
        Transliterator.getInstance("Han-Latin; Latin-ASCII")
    }

    private val formsCache = object : LinkedHashMap<String, SearchForms>(256, 0.75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, SearchForms>?): Boolean =
            size > 512
    }

    private fun normalize(value: String): String = buildString {
        value.lowercase(Locale.ROOT).codePoints().forEach { codePoint ->
            if (Character.isLetterOrDigit(codePoint)) appendCodePoint(codePoint)
        }
    }

    private fun formsFor(value: String): SearchForms = synchronized(formsCache) {
        formsCache[value]?.let { return it }
        val syllables = transliterator.get()!!.transliterate(value)
            .lowercase(Locale.ROOT)
            .split(Regex("[^a-z0-9]+"))
            .filter(String::isNotBlank)
        SearchForms(
            original = normalize(value),
            fullPinyin = syllables.joinToString(""),
            initials = syllables.joinToString("") { it.take(1) },
        ).also { formsCache[value] = it }
    }

    private fun isSubsequence(query: String, target: String): Boolean {
        val wanted = query.codePoints().toArray()
        var index = 0
        target.codePoints().forEach { codePoint ->
            if (index < wanted.size && codePoint == wanted[index]) index++
        }
        return index == wanted.size
    }

    fun matches(query: String, vararg fields: String): Boolean {
        val normalizedQuery = normalize(query.trim())
        if (normalizedQuery.isEmpty()) return true

        return fields.any { field ->
            val forms = formsFor(field)
            forms.original.contains(normalizedQuery)
                || isSubsequence(normalizedQuery, forms.original)
                || forms.fullPinyin.contains(normalizedQuery)
                || forms.initials.contains(normalizedQuery)
        }
    }
}
