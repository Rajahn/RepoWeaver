package com.example.overloads;

import java.io.Reader;
import java.lang.reflect.Type;

public class Codec {
    public String fromJson(String json, Class<?> clazz) {
        return json;
    }

    public String fromJson(Reader reader, Type type) {
        return null;
    }

    public String fromJson(String json, Token token) {
        return json;
    }

    public void write(String value) {
    }

    public void write(StringBuilder value) {
    }

    public void tag(Object o) {
    }

    public void tag(String s) {
    }

    public void accept(Foo f) {
    }

    public void accept(Bar b) {
    }
}
