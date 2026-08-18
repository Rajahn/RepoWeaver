package com.example.m3typed;

public class Box<T> {
    private final T value;

    public Box(T value) {
        this.value = value;
    }

    public T identity(T input) {
        return input;
    }
}
